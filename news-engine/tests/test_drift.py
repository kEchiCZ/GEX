"""Testy drift hlídky (#403): binomický test, nálezy, anti-spam alertů."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.engine import Engine

from gexlens_engine.storage.meta import meta_metadata
from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    news_events,
    news_model_stats,
    news_reactions,
)
from gexlens_engine.storage.setups_store import SetupsRepository
from gexlens_news.drift import RECENT_N, DriftJob, binomial_p_at_most

NOW = dt.datetime(2026, 7, 31, 2, 0, tzinfo=dt.UTC)


def test_binomial_p_at_most() -> None:
    assert binomial_p_at_most(0, 10, 0.5) == pytest.approx(0.5**10)
    assert binomial_p_at_most(10, 10, 0.5) == pytest.approx(1.0)
    # 5 zásahů z 20 při dlouhodobých 61 % je významný pokles
    assert binomial_p_at_most(5, 20, 0.61) < 0.01
    # 11 z 20 při 61 % je v normě
    assert binomial_p_at_most(11, 20, 0.61) > 0.2
    assert binomial_p_at_most(3, 0, 0.5) == 1.0  # degenerované vstupy


def make_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'drift.sqlite'}")
    ensure_sentiment_schema(engine)
    meta_metadata.create_all(engine)
    SetupsRepository(engine).ensure_schema()
    return engine


def seed_bucket(engine: Engine, *, hit_rate: float) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(news_model_stats),
            [
                {
                    "regime": "all",
                    "category": "FED",
                    "importance": 3,
                    "surprise_bucket": "none",
                    "deferred": False,
                    "window_min": 5,
                    "symbol": "ES",
                    "n": 100,
                    "ret_mean_bp": 5.0,
                    "ret_median_bp": 5.0,
                    "ret_sigma_bp": 3.0,
                    "hit_rate": hit_rate,
                    "hit_rate_lb": 0.55,
                    "computed_at": NOW,
                }
            ],
        )


def seed_recent_reactions(engine: Engine, *, hits: int, total: int) -> None:
    """Posledních `total` reakcí bucketu: `hits` ve směru klasifikace."""
    with engine.begin() as conn:
        for index in range(total):
            event_id = 1000 + index
            hit = index < hits
            conn.execute(
                insert(news_events),
                [
                    {
                        "id": event_id,
                        "ts_event": NOW - dt.timedelta(hours=index + 1),
                        "ts_ingested": NOW,
                        "source": "rss_news",
                        "kind": "headline",
                        "category": "FED",
                        "importance": 3,
                        "title": f"e{event_id}",
                        "symbols": ["ES"],
                        "market_closed": False,
                        "sentiment_dir": 1,
                        "dedup_hash": f"h{event_id}",
                        "raw": {},
                    }
                ],
            )
            conn.execute(
                insert(news_reactions),
                [
                    {
                        "event_id": event_id,
                        "symbol": "ES",
                        "window_min": 5,
                        "ret_bp": 4.0 if hit else -4.0,
                        "range_bp": 6.0,
                        "vol_z": None,
                        "contaminated": False,
                        "deferred": False,
                        "computed_at": NOW,
                    }
                ],
            )


def test_drift_fires_once_for_degraded_bucket(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_bucket(engine, hit_rate=0.61)
    seed_recent_reactions(engine, hits=5, total=RECENT_N)  # 25 % vs. 61 %
    job = DriftJob(engine)

    alerts = job.run(NOW)
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "drift"
    assert "FED" in alerts[0]["message"]
    assert "25 %" in alerts[0]["message"] or "25%" in alerts[0]["message"]

    # Táž situace další noc → nález trvá, ale alert se neopakuje (anti-spam)
    assert job.run(NOW + dt.timedelta(days=1)) == []


def test_no_drift_when_recent_matches_history(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_bucket(engine, hit_rate=0.61)
    seed_recent_reactions(engine, hits=12, total=RECENT_N)  # 60 % ≈ 61 %
    assert DriftJob(engine).run(NOW) == []


def test_no_drift_without_enough_recent_samples(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_bucket(engine, hit_rate=0.61)
    seed_recent_reactions(engine, hits=0, total=5)  # málo dat → žádný test
    assert DriftJob(engine).run(NOW) == []


def test_setup_template_drift(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    repository = SetupsRepository(engine)
    # 50 uzavřených: starších 30 se 70% úspěšností, posledních 20 jen 4 výhry
    outcomes = [True] * 21 + [False] * 9 + [True] * 4 + [False] * 16
    for index, win in enumerate(outcomes):
        setup_id = repository.create(
            symbol="ES",
            expiry="20260731",
            template="wall_bounce",
            direction="long",
            created_ts=NOW - dt.timedelta(days=len(outcomes) - index),
            entry=7400.0,
            target=7420.0,
            stop=7390.0,
            confidence=1,
            reason="test",
            context={},
        )
        repository.close(
            setup_id,
            status="closed_target" if win else "closed_stop",
            closed_ts=NOW - dt.timedelta(days=len(outcomes) - index, hours=-2),
            outcome_r=1.0 if win else -1.0,
            mfe=1.0,
            mae=0.5,
        )
    alerts = DriftJob(engine).run(NOW)
    assert len(alerts) == 1
    assert "wall_bounce" in alerts[0]["message"]
