"""Testy anomálních reakcí (#295, SPEC 9.4): p90 práh, hloubka bucketu, dedup."""

import datetime as dt
from pathlib import Path

from sqlalchemy import create_engine, insert
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    news_events,
    news_reactions,
)
from gexlens_news.anomaly_job import AnomalyJob, percentile_abs

NOW = dt.datetime(2026, 7, 29, 16, 0, tzinfo=dt.UTC)
START = NOW - dt.timedelta(hours=1)


def make_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine


def seed_event(engine: Engine, event_id: int, *, title: str = "CPI") -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(news_events),
            [
                {
                    "id": event_id,
                    "ts_event": NOW - dt.timedelta(minutes=30),
                    "ts_ingested": NOW,
                    "source": "forexfactory",
                    "kind": "scheduled",
                    "category": "MACRO_INFLATION",
                    "importance": 3,
                    "title": title,
                    "symbols": ["ES"],
                    "market_closed": False,
                    "dedup_hash": f"hash-{event_id}",
                    "raw": {},
                }
            ],
        )


def seed_reaction(
    engine: Engine,
    event_id: int,
    *,
    ret_bp: float,
    computed_at: dt.datetime,
    contaminated: bool = False,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(news_reactions),
            [
                {
                    "event_id": event_id,
                    "symbol": "ES",
                    "window_min": 5,
                    "ret_bp": ret_bp,
                    "range_bp": abs(ret_bp) + 2,
                    "vol_z": None,
                    "contaminated": contaminated,
                    "deferred": False,
                    "computed_at": computed_at,
                }
            ],
        )


def seed_history(engine: Engine, count: int, *, ret_bp: float = 5.0) -> None:
    """Historický bucket: `count` eventů se stejnou (mírnou) reakcí."""
    for offset in range(count):
        event_id = 1000 + offset
        seed_event(engine, event_id)
        seed_reaction(engine, event_id, ret_bp=ret_bp, computed_at=START - dt.timedelta(days=1))


def test_percentile_abs_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]  # 1..100
    assert percentile_abs(values, 0.90) == 90.0
    assert percentile_abs([-8.0, 3.0], 0.90) == 8.0  # bere absolutní hodnoty


def test_reaction_above_p90_fires_once(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_history(engine, 40)  # p90 = 5 bp
    seed_event(engine, 1, title="CPI hot")
    seed_reaction(engine, 1, ret_bp=-42.0, computed_at=NOW)
    job = AnomalyJob(engine, started_at=START)

    alerts = job.run(NOW)
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "news_anomaly"
    assert alerts[0]["symbol"] == "ES"
    assert "CPI hot" in alerts[0]["message"]
    assert alerts[0]["ts"] == int(NOW.timestamp())
    # Táž reakce se podruhé nehlásí
    assert job.run(NOW + dt.timedelta(minutes=5)) == []


def test_shallow_bucket_and_mild_reaction_stay_quiet(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_history(engine, 10)  # pod MIN_BUCKET_SAMPLES
    seed_event(engine, 1)
    seed_reaction(engine, 1, ret_bp=42.0, computed_at=NOW)
    assert AnomalyJob(engine, started_at=START).run(NOW) == []

    # Hluboký bucket, ale reakce pod p90 → ticho
    deep_dir = tmp_path / "deep"
    deep_dir.mkdir()
    engine2 = make_db(deep_dir)
    seed_history(engine2, 40, ret_bp=10.0)
    seed_event(engine2, 1)
    seed_reaction(engine2, 1, ret_bp=6.0, computed_at=NOW)
    assert AnomalyJob(engine2, started_at=START).run(NOW) == []


def test_old_and_contaminated_reactions_ignored(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_history(engine, 40)
    # Reakce spočítaná před startem procesu — trader ji už viděl
    seed_event(engine, 1)
    seed_reaction(engine, 1, ret_bp=42.0, computed_at=START - dt.timedelta(minutes=5))
    # Kontaminované okno neměří reakci na tuhle zprávu (SPEC 2.4)
    seed_event(engine, 2)
    seed_reaction(engine, 2, ret_bp=42.0, computed_at=NOW, contaminated=True)
    assert AnomalyJob(engine, started_at=START).run(NOW) == []
