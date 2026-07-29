"""Testy pravidlové klasifikace (#280, #373): verzování a výběr fronty."""

import datetime as dt
from pathlib import Path

from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    news_classifications,
    news_events,
)
from gexlens_news.classification_job import RuleClassificationJob

NOW = dt.datetime(2026, 7, 29, 16, 0, tzinfo=dt.UTC)


def make_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine


def seed_event(engine: Engine, event_id: int, *, kind: str = "headline") -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(news_events),
            [
                {
                    "id": event_id,
                    "ts_event": NOW - dt.timedelta(minutes=event_id),
                    "ts_ingested": NOW,
                    "source": "rss_news",
                    "kind": kind,
                    "title": f"FOMC statement {event_id}",
                    "symbols": [],
                    "market_closed": False,
                    "dedup_hash": f"hash-{event_id}",
                    "raw": {},
                }
            ],
        )


def seed_llm_classification(engine: Engine, event_id: int) -> None:
    from sqlalchemy import update

    with engine.begin() as conn:
        conn.execute(
            insert(news_classifications),
            [
                {
                    "event_id": event_id,
                    "version": 1,
                    "source": "llm",
                    "category": "FED",
                    "importance": 3,
                    "direction": -1,
                    "strength": 0.8,
                    "created_at": NOW - dt.timedelta(minutes=1),
                }
            ],
        )
        # LLM pass denormalizuje do news_events — zrcadlí llm_classifier
        conn.execute(
            update(news_events)
            .where(news_events.c.id == event_id)
            .values(category="FED", importance=3, sentiment_dir=-1, sentiment_source="llm")
        )


def test_events_with_any_classification_are_skipped(tmp_path: Path) -> None:
    """#373: LLM po backfillu předběhl pravidlový pass — event s LLM verzí 1
    nesmí do pravidlové fronty (natvrdo psaná verze 1 shazovala celou dávku
    na UniqueViolation a klasifikace stála)."""
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_llm_classification(engine, 1)
    seed_event(engine, 2)

    job = RuleClassificationJob(engine)
    assert job.run(NOW) == 1  # jen event 2; event 1 s LLM verzí se přeskočí

    with engine.connect() as conn:
        stored = conn.execute(
            select(
                news_classifications.c.event_id,
                news_classifications.c.version,
                news_classifications.c.source,
            ).order_by(news_classifications.c.event_id)
        ).fetchall()
        event1 = conn.execute(select(news_events).where(news_events.c.id == 1)).one()
    assert [(r.event_id, r.version, r.source) for r in stored] == [(1, 1, "llm"), (2, 1, "rule")]
    # Denormalizace eventu 1 zůstala z LLM — pravidlový pass ji neregresoval
    assert event1.sentiment_source == "llm"

    # Druhý běh: fronta prázdná, žádný pád, žádné duplicity
    assert job.run(NOW) == 0


def test_scheduled_events_always_get_rule_pass(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_event(engine, 1, kind="scheduled")
    job = RuleClassificationJob(engine)
    assert job.run(NOW) == 1
    with engine.connect() as conn:
        row = conn.execute(select(news_classifications)).one()
    assert row.source == "rule"
    assert row.version == 1
