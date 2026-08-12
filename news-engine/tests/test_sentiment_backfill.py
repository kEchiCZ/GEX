"""Testy backfillu denních svíček SentIndexu (#375)."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    news_events,
    sentiment_daily,
)
from gexlens_news.sentiment_backfill import backfill_sentiment_daily

DAY1 = dt.date(2026, 7, 20)
DAY2 = dt.date(2026, 7, 21)


def make_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine


def seed_scored_event(engine: Engine, event_id: int, ts: dt.datetime, score: float) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(news_events),
            [
                {
                    "id": event_id,
                    "ts_event": ts,
                    "ts_ingested": ts,
                    "source": "forexfactory",
                    "kind": "scheduled",
                    "category": "MACRO_INFLATION",
                    "importance": 3,
                    "title": f"USD CPI m/m {event_id}",
                    "symbols": ["ES"],
                    "market_closed": False,
                    "sentiment_dir": 1 if score > 0 else -1,
                    "sentiment_score": score,
                    "sentiment_source": "rule",
                    "dedup_hash": f"hash-{event_id}",
                    "raw": {},
                }
            ],
        )


def test_backfill_writes_days_from_first_event(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    # Event ráno DAY1: kladný impuls → open dne ~0, close dne > 0 nemusí
    # platit (decay), ale high > 0 určitě; DAY2 už jen dozvuk
    seed_scored_event(engine, 1, dt.datetime(2026, 7, 20, 13, 30, tzinfo=dt.UTC), 0.6)

    stats = backfill_sentiment_daily(engine, end=DAY2, step_minutes=5, symbols=("ES",))
    assert stats.days_written == 2  # DAY1 + DAY2 (dozvuk)
    assert stats.days_skipped == 0

    with engine.connect() as conn:
        rows = conn.execute(select(sentiment_daily).order_by(sentiment_daily.c.date)).fetchall()
    assert [row.date for row in rows] == [DAY1, DAY2]
    day1 = rows[0]
    assert float(day1.open) == pytest.approx(0.0)  # před eventem index nulový
    assert float(day1.high) == pytest.approx(0.6, abs=0.01)  # špička při eventu
    # DAY2 high = dozvuk na půlnoci: 0.6 × 2^(−630 min / 180 min) ≈ 0.053
    assert float(rows[1].high) == pytest.approx(0.6 * 2 ** (-630 / 180), abs=0.005)


def test_backfill_never_overwrites_live_days(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_scored_event(engine, 1, dt.datetime(2026, 7, 20, 13, 30, tzinfo=dt.UTC), 0.6)
    # Živě spočítaný den s odlišnou hodnotou
    with engine.begin() as conn:
        conn.execute(
            insert(sentiment_daily),
            [
                {
                    "date": DAY1,
                    "symbol": "ES",
                    "open": 9.0,
                    "high": 9.0,
                    "low": 9.0,
                    "close": 9.0,
                    "update_time": dt.datetime(2026, 7, 20, 23, 59, tzinfo=dt.UTC),
                }
            ],
        )

    stats = backfill_sentiment_daily(engine, end=DAY2, step_minutes=5, symbols=("ES",))
    assert stats.days_skipped == 1
    assert stats.days_written == 1  # jen DAY2

    with engine.connect() as conn:
        live = conn.execute(
            select(sentiment_daily.c.close).where(sentiment_daily.c.date == DAY1)
        ).scalar()
    assert live is not None
    assert float(live) == pytest.approx(9.0)  # živý den nedotčen


def test_backfill_without_events_is_noop(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    stats = backfill_sentiment_daily(engine)
    assert stats.days_written == 0


def test_backfill_pise_radu_pro_kazdy_symbol(tmp_path: Path) -> None:
    """ADR-0026: NQ řada vzniká vedle ES — z týchž eventů, s NQ vahami."""
    engine = make_db(tmp_path)
    seed_scored_event(engine, 1, dt.datetime(2026, 7, 20, 13, 30, tzinfo=dt.UTC), 0.6)

    stats = backfill_sentiment_daily(engine, end=DAY2, step_minutes=5)
    assert stats.days_written == 4  # 2 dny × 2 symboly

    with engine.connect() as conn:
        rows = conn.execute(select(sentiment_daily)).fetchall()
    assert {row.symbol for row in rows} == {"ES", "NQ"}
    # Bez vah v news_weights jsou řady shodné (neutrální 1.0) — liší se až
    # s per-symbol vahami; tady se ověřuje existence obou řad
    es = sorted(float(r.close) for r in rows if r.symbol == "ES")
    nq = sorted(float(r.close) for r in rows if r.symbol == "NQ")
    assert es == nq
