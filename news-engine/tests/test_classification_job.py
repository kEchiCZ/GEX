"""Testy pravidlové klasifikace (#280, #373): verzování a výběr fronty."""

import datetime as dt
import logging
from pathlib import Path

import pytest
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


def seed_event(
    engine: Engine,
    event_id: int,
    *,
    kind: str = "headline",
    ts_ingested: dt.datetime = NOW,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(news_events),
            [
                {
                    "id": event_id,
                    "ts_event": NOW - dt.timedelta(minutes=event_id),
                    "ts_ingested": ts_ingested,
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


def classified_ids(engine: Engine) -> list[tuple[int, int, str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                news_classifications.c.event_id,
                news_classifications.c.version,
                news_classifications.c.source,
            ).order_by(news_classifications.c.event_id, news_classifications.c.version)
        ).fetchall()
    return [(int(r.event_id), int(r.version), str(r.source)) for r in rows]


def test_fast_path_classifies_only_rows_above_watermark(tmp_path: Path) -> None:
    """#1496: první `run_new` jen nastaví watermark; co bylo v DB před
    startem, dojede pojistka `run`, rychlá cesta bere jen nové řádky."""
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    job = RuleClassificationJob(engine)
    assert job.run_new(NOW) == []
    assert classified_ids(engine) == []

    seed_event(engine, 2)
    batch = job.run_new(NOW + dt.timedelta(seconds=1))
    assert [item["id"] for item in batch] == [2]
    assert batch[0]["category"] == "FED"
    assert classified_ids(engine) == [(2, 1, "rule")]

    # Bez nových řádků prázdno; starší event 1 vezme pojistka
    assert job.run_new(NOW + dt.timedelta(seconds=2)) == []
    assert job.run(NOW) == 1
    assert classified_ids(engine) == [(1, 1, "rule"), (2, 1, "rule")]


def test_row_committed_below_watermark_goes_to_safety_net(tmp_path: Path) -> None:
    """#1496: řádek s nižším `id` commitnutý až po posunu watermarku (dlouhá
    transakce) rychlá cesta mine — pojistka ho klasifikuje."""
    engine = make_db(tmp_path)
    job = RuleClassificationJob(engine)
    job.run_new(NOW)  # watermark 0
    seed_event(engine, 6)
    assert [item["id"] for item in job.run_new(NOW)] == [6]
    seed_event(engine, 4)  # pozdní commit s nižším id
    assert job.run_new(NOW) == []
    assert job.run(NOW) == 1
    assert classified_ids(engine) == [(4, 1, "rule"), (6, 1, "rule")]


def test_fast_path_and_safety_net_never_classify_twice(tmp_path: Path) -> None:
    """#1496: obě cesty běží v různých vláknech — event, který mezi výběrem
    a zápisem klasifikovala druhá cesta, se přeskočí (žádná verze 2 ani
    UniqueViolation)."""
    engine = make_db(tmp_path)
    job = RuleClassificationJob(engine)
    job.run_new(NOW)
    seed_event(engine, 1)
    stale = job._pending(500)  # pojistka vybrala event 1 …
    assert [item["id"] for item in job.run_new(NOW)] == [1]  # … rychlá cesta ho zapsala
    assert job._classify(stale, NOW) == []  # zápis pojistky ho přeskočí
    assert classified_ids(engine) == [(1, 1, "rule")]

    seed_event(engine, 2)
    assert job.run(NOW) == 1  # opačné pořadí: pojistka první
    assert job.run_new(NOW) == []
    assert classified_ids(engine) == [(1, 1, "rule"), (2, 1, "rule")]


def test_pushed_row_carries_real_ingest_time(tmp_path: Path) -> None:
    """#1496: push do WS nesl `ts_event` místo `ts_ingested` — karta z něj
    počítá zpoždění zdroje."""
    engine = make_db(tmp_path)
    job = RuleClassificationJob(engine)
    job.run_new(NOW)
    ingested = NOW + dt.timedelta(minutes=3)
    seed_event(engine, 1, ts_ingested=ingested)
    (pushed,) = job.run_new(ingested + dt.timedelta(seconds=1))
    assert pushed["ts_ingested"] == ingested.isoformat()
    assert pushed["ts_event"] != pushed["ts_ingested"]


def test_fast_path_logs_latency_every_ten_minutes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    engine = make_db(tmp_path)
    job = RuleClassificationJob(engine)
    job.run_new(NOW)
    seed_event(engine, 1, ts_ingested=NOW)
    caplog.set_level(logging.INFO, logger="gexlens_news.classification_job")
    job.run_new(NOW + dt.timedelta(seconds=2))
    assert "Rychlá klasifikace" not in caplog.text  # ještě neuplynulo 10 min
    job.run_new(NOW + dt.timedelta(minutes=10))
    assert "medián 2.0 s" in caplog.text
