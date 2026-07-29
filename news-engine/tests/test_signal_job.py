"""Testy SignalJob (#294): zápis, dedup, expirace stavem, outcomes."""

import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    news_events,
    news_model_stats,
    signal_outcomes,
    signals,
)
from gexlens_news.signal_job import SignalJob, load_gex_context

NOW = dt.datetime(2026, 7, 29, 14, 0, tzinfo=dt.UTC)


def make_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine


def seed_event(engine: Engine, event_id: int, *, score: float = 0.6) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(news_events),
            [
                {
                    "id": event_id,
                    "ts_event": NOW - dt.timedelta(minutes=10),
                    "ts_ingested": NOW,
                    "source": "rss_news",
                    "kind": "headline",
                    "category": "FED",
                    "importance": 3,
                    "title": "Fed signals pause",
                    "symbols": ["ES"],
                    "market_closed": False,
                    "sentiment_dir": 1 if score > 0 else -1,
                    "sentiment_score": score,
                    "sentiment_source": "llm",
                    "dedup_hash": f"hash-{event_id}",
                    "raw": {},
                }
            ],
        )


def seed_bucket(engine: Engine, *, n: int = 50, lb: float = 0.6, mean: float = 6.0) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(news_model_stats),
            [
                {
                    "category": "FED",
                    "importance": 3,
                    "surprise_bucket": "none",
                    "deferred": False,
                    "window_min": 5,
                    "symbol": "ES",
                    "n": n,
                    "ret_mean_bp": mean,
                    "ret_median_bp": mean,
                    "ret_sigma_bp": 4.0,
                    "hit_rate": 0.65,
                    "hit_rate_lb": lb,
                    "computed_at": NOW,
                }
            ],
        )


def write_bars(tmp_path: Path, closes: list[tuple[dt.datetime, float]]) -> None:
    path = tmp_path / "derived" / "ES" / "bars" / f"{NOW.date().isoformat()}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "ts_min": pa.array([c[0] for c in closes], pa.timestamp("us", tz="UTC")),
            "open": pa.array([c[1] for c in closes]),
            "high": pa.array([c[1] for c in closes]),
            "low": pa.array([c[1] for c in closes]),
            "close": pa.array([c[1] for c in closes]),
            "volume": pa.array([0.0] * len(closes)),
        }
    )
    pq.write_table(table, path)


def test_signal_created_once_and_deduped(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_bucket(engine)
    job = SignalJob(engine, tmp_path, symbols=("ES",))

    assert job.run(NOW, state="RiskOn") == 1
    assert len(job.last_created) == 1
    created = job.last_created[0]
    assert created["direction"] == "long"
    assert created["mode"] == "NEWS"

    # Týž event podruhé → dedup, nic nového
    assert job.run(NOW + dt.timedelta(minutes=5), state="RiskOn") == 0

    with engine.connect() as conn:
        rows = conn.execute(select(signals)).fetchall()
    assert len(rows) == 1
    assert rows[0].inputs["event_id"] == 1


def test_no_signals_in_neutral_or_below_gate(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_bucket(engine, n=10)  # pod gate
    job = SignalJob(engine, tmp_path, symbols=("ES",))
    assert job.run(NOW, state="RiskOn") == 0

    seed_bucket_ok = SignalJob(engine, tmp_path, symbols=("ES",))
    assert seed_bucket_ok.run(NOW, state="Neutral") == 0  # Neutral negeneruje


def test_confirmed_state_change_expires_active_signals(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_bucket(engine)
    job = SignalJob(engine, tmp_path, symbols=("ES",))
    assert job.run(NOW, state="RiskOn") == 1

    # Potvrzená změna stavu → aktivní signál expiruje okamžitě (SPEC 6.3)
    later = NOW + dt.timedelta(minutes=30)
    job.run(later, state="Neutral")
    with engine.connect() as conn:
        row = conn.execute(select(signals)).one()
    expiry = row.expiry_ts if row.expiry_ts.tzinfo else row.expiry_ts.replace(tzinfo=dt.UTC)
    assert expiry == later


def test_outcomes_measured_from_bars_after_windows_close(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_bucket(engine)
    # Bary: cena 7450 při signálu, 7460 po 5 minutách → +13.4 bp → long correct
    write_bars(
        tmp_path,
        [
            (NOW - dt.timedelta(minutes=1), 7450.0),
            (NOW + dt.timedelta(minutes=1), 7452.0),
            (NOW + dt.timedelta(minutes=5), 7460.0),
        ],
    )
    job = SignalJob(engine, tmp_path, symbols=("ES",))
    assert job.run(NOW, state="RiskOn") == 1

    # Po uzavření okna +5 min se outcome spočítá; delší okna ještě ne
    job.run(NOW + dt.timedelta(minutes=6), state="RiskOn")
    with engine.connect() as conn:
        outcomes = conn.execute(select(signal_outcomes)).fetchall()
    by_window = {int(o.window_min): o for o in outcomes}
    assert 5 in by_window
    assert 60 not in by_window
    outcome = by_window[5]
    assert outcome.correct is True or outcome.correct == 1
    assert float(outcome.ret_bp) == pytest.approx((7460.0 - 7450.0) / 7450.0 * 10_000, rel=1e-6)


def test_gex_context_loader(tmp_path: Path) -> None:
    # Bez barů → žádný kontext
    assert load_gex_context(tmp_path, "ES", NOW) is None

    write_bars(tmp_path, [(NOW - dt.timedelta(minutes=1), 7455.0)])
    levels_path = (
        tmp_path / "derived" / "ES" / "20260729" / "levels" / f"{NOW.date().isoformat()}.parquet"
    )
    levels_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "ts_min": pa.array([NOW - dt.timedelta(minutes=2)], pa.timestamp("us", tz="UTC")),
                "flip": pa.array([7440.0]),
            }
        ),
        levels_path,
    )
    context = load_gex_context(tmp_path, "ES", NOW)
    assert context is not None
    assert context.spot == 7455.0
    assert context.flip == 7440.0
    assert context.supports("long")  # spot nad flipem
