"""Testy review fronty (#293, SPEC 5.7): kritéria, dedup, auto-uzavření."""

import datetime as dt
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    ReactionWindow,
    ensure_sentiment_schema,
    news_classifications,
    news_events,
    news_model_stats,
    news_reactions,
    reaction_row_values,
    review_queue,
)
from gexlens_news.review_job import ReviewJob

NOW = dt.datetime(2026, 7, 29, 16, 0, tzinfo=dt.UTC)


def make_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine


def seed_event_with_llm(
    engine: Engine,
    event_id: int,
    *,
    direction: int = 1,
    strength: float = 0.8,
    importance: int = 3,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(news_events),
            [
                {
                    "id": event_id,
                    "ts_event": NOW - dt.timedelta(hours=1),
                    "ts_ingested": NOW,
                    "source": "rss_news",
                    "kind": "headline",
                    "category": "FED",
                    "importance": importance,
                    "title": f"Fed event {event_id}",
                    "symbols": ["ES"],
                    "market_closed": False,
                    "sentiment_dir": direction,
                    "sentiment_score": direction * strength,
                    "sentiment_source": "llm",
                    "dedup_hash": f"hash-{event_id}",
                    "raw": {},
                }
            ],
        )
        conn.execute(
            insert(news_classifications),
            [
                {
                    "event_id": event_id,
                    "version": 1,
                    "source": "llm",
                    "category": "FED",
                    "importance": importance,
                    "direction": direction,
                    "strength": strength,
                    "created_at": NOW - dt.timedelta(minutes=55),
                }
            ],
        )


def seed_bucket_mean(engine: Engine, *, mean: float, n: int = 40) -> None:
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
                    "ret_sigma_bp": 3.0,
                    "hit_rate": 0.6,
                    "hit_rate_lb": 0.55,
                    "computed_at": NOW,
                }
            ],
        )


def queue_rows(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(select(review_queue)).fetchall())


def test_disagreement_enqueues_and_dedups(tmp_path: Path) -> None:
    """LLM říká +1, empirický bucket měří záporný průměr → rozpor (imp ≥ 2)."""
    engine = make_db(tmp_path)
    seed_event_with_llm(engine, 1, direction=1, strength=0.8)
    seed_bucket_mean(engine, mean=-4.0)
    job = ReviewJob(engine)

    assert job.run(NOW) == 1
    rows = queue_rows(engine)
    assert len(rows) == 1
    assert rows[0].reason == "disagreement"
    # Druhý běh nepřidá duplicitu
    assert job.run(NOW) == 0


def test_agreement_or_shallow_bucket_does_not_enqueue(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_event_with_llm(engine, 1, direction=1, strength=0.8)
    seed_bucket_mean(engine, mean=4.0)  # souhlas
    assert ReviewJob(engine).run(NOW) == 0

    seed_event_with_llm(engine, 2, direction=-1, strength=0.8)
    # Bucket pro směr −1 by byl rozpor (mean +4), ale n=5 < 10 → není „model"
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    engine2 = make_db(second_dir)
    seed_event_with_llm(engine2, 1, direction=-1, strength=0.8)
    seed_bucket_mean(engine2, mean=4.0, n=5)
    assert ReviewJob(engine2).run(NOW) == 0


def test_low_confidence_enqueues_only_important(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_event_with_llm(engine, 1, direction=1, strength=0.2)  # pod 0.3
    seed_event_with_llm(engine, 2, direction=1, strength=0.2, importance=1)  # imp 1
    job = ReviewJob(engine)
    assert job.run(NOW) == 1
    rows = queue_rows(engine)
    assert rows[0].event_id == 1
    assert rows[0].reason == "low_confidence"


def test_auto_resolve_after_longest_window_measured(tmp_path: Path) -> None:
    """SPEC 5.7: bez ručního zásahu se položka po uzavření oken vyhodnotí."""
    engine = make_db(tmp_path)
    seed_event_with_llm(engine, 1, direction=1, strength=0.2)
    job = ReviewJob(engine)
    job.run(NOW)
    assert queue_rows(engine)[0].resolved_at is None

    # Reakce nejdelšího okna (60 min) existuje → auto-uzavření
    longest = ReactionWindow(
        window_min=60,
        ret_bp=5.0,
        range_bp=8.0,
        vol_z=None,
        contaminated=False,
        deferred=False,
        gex_regime=None,
        computed_at=NOW,
    )
    with engine.begin() as conn:
        conn.execute(
            insert(news_reactions).values(event_id=1, symbol="ES", **reaction_row_values([longest]))
        )
    job.run(NOW + dt.timedelta(minutes=5))
    assert queue_rows(engine)[0].resolved_at is not None
