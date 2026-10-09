"""Backfill tieru obsahu do historie (ADR-0059, #1486).

Skript žije ve `scripts/`, načítá se přes importlib jako ostatní skripty.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import ensure_sentiment_schema, news_events

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_content_tier.py"
#: První post s příznakem kurátora (#1291 nasazeno 25. 9.)
CUTOVER = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.UTC)
BEFORE = CUTOVER - dt.timedelta(days=3)
AFTER = CUTOVER + dt.timedelta(days=3)
LISTED = "did:plc:listed"


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("backfill_content_tier", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclassy skriptu (odložené anotace) hledají svůj modul v sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    rows = [
        ("alpaca", BEFORE, {"author": "Benzinga Newsdesk"}),
        ("alpaca", BEFORE, {"author": "Benzinga Insights"}),
        ("bluesky", BEFORE, {"did": LISTED}),  # před příznakem, autor ze seznamu → kurátor
        ("bluesky", BEFORE, {"did": "did:plc:other"}),
        ("bluesky", CUTOVER, {"did": "did:plc:flagged", "curated": True}),
        ("bluesky", AFTER, {"did": LISTED}),  # po příznaku rozhoduje jen příznak
        ("forexfactory", BEFORE, {"impact": "High"}),
        ("ibkr_brfg", BEFORE, {"provider": "BRFG"}),
    ]
    with engine.begin() as conn:
        for index, (source, ingested, raw) in enumerate(rows):
            conn.execute(
                insert(news_events).values(
                    ts_event=ingested,
                    ts_ingested=ingested,
                    source=source,
                    kind="headline",
                    title=f"zpráva {index}",
                    symbols=[],
                    market_closed=False,
                    dedup_hash=f"h{index}",
                    raw=raw,
                )
            )
    return engine


def tiers(engine: Engine) -> list[int | None]:
    with engine.connect() as conn:
        return [
            row.content_tier
            for row in conn.execute(select(news_events.c.content_tier).order_by(news_events.c.id))
        ]


def test_backfill_maps_history_and_is_idempotent(tmp_path: Path) -> None:
    script = load_script()
    engine = make_engine(tmp_path)
    curated = frozenset({LISTED, "did:plc:flagged"})

    report = script.backfill(engine, curated, apply=True, batch=3)

    assert tiers(engine) == [2, 3, 2, None, 2, None, None, 3]
    # forexfactory a nekurátoři zůstávají NULL — to není změna
    assert report.changed == 5
    assert report.curated_by_list == 1
    assert report.tiers[("bluesky", 2)] == 2
    assert report.tiers[("bluesky", None)] == 2
    # Druhý běh nic nemění
    assert script.backfill(engine, curated, apply=True, batch=3).changed == 0


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    script = load_script()
    engine = make_engine(tmp_path)

    report = script.backfill(engine, frozenset({LISTED}), apply=False)

    assert report.changed == 5
    assert tiers(engine) == [None] * 8


def test_without_any_flag_all_bluesky_use_the_list(tmp_path: Path) -> None:
    """Bez jediného postu s příznakem je celá historie „před příznakem“."""
    script = load_script()
    engine = make_engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(news_events.delete().where(news_events.c.dedup_hash == "h4"))

    script.backfill(engine, frozenset({LISTED}), apply=True)

    # LISTED před i po (bývalém) cutoveru → kurátor podle seznamu
    assert tiers(engine) == [2, 3, 2, None, 2, None, 3]
