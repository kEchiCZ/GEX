"""Oprava HTML entit v historii titulků Alpaca a Bluesky (#1500).

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

from gexlens_engine.compute.newstext import dedup_hash
from gexlens_engine.storage.sentiment import ensure_sentiment_schema, news_events

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fix_news_html_entities.py"
TS = dt.datetime(2026, 10, 9, 11, 47, tzinfo=dt.UTC)

#: (zdroj, titulek, perex, uložený hash) — `old-*` = hash z doby před opravou
ROWS: list[tuple[str, str, str | None, str]] = [
    ("alpaca", "What&#39;s Going On With Vistra Stock?", None, "old-0"),
    ("alpaca", "Fed holds rates", "S&amp;P 500 flat", "keep-1"),
    ("bluesky", "MOC IMBALANCE S&amp;P 500: 1904 MLN", None, "old-2"),
    ("alpaca", "S&P 500 hits record", None, "keep-3"),
    # Kopie, kterou dedup kvůli entitě minul: čistý titulek už má rss_news
    ("alpaca", "Johnson &amp; Johnson Slides", None, "old-4"),
    ("rss_news", "Johnson & Johnson Slides", None, dedup_hash("Johnson & Johnson Slides", TS)),
    # Zdroj bez opraveného collectoru se nemění
    ("finnhub", "Fed&#39;s Waller", None, "keep-6"),
]


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("fix_news_html_entities", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclassy skriptu (odložené anotace) hledají svůj modul v sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    with engine.begin() as conn:
        for source, title, summary, stored_hash in ROWS:
            conn.execute(
                insert(news_events).values(
                    ts_event=TS,
                    ts_ingested=TS,
                    source=source,
                    kind="headline",
                    title=title,
                    summary=summary,
                    symbols=[],
                    market_closed=False,
                    dedup_hash=stored_hash,
                    raw={"author": "x"},
                )
            )
    return engine


def stored(engine: Engine) -> list[tuple[str, str | None, str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(news_events.c.title, news_events.c.summary, news_events.c.dedup_hash).order_by(
                news_events.c.id
            )
        ).fetchall()
    return [(row.title, row.summary, row.dedup_hash) for row in rows]


def test_apply_dekoduje_prepocte_hash_a_je_idempotentni(tmp_path: Path) -> None:
    script = load_script()
    engine = make_engine(tmp_path)

    report = script.fix(engine, apply=True, batch=2)

    after = stored(engine)
    vistra = "What's Going On With Vistra Stock?"
    assert after[0] == (vistra, None, dedup_hash(vistra, TS))
    # Jen perex: titulek a hash beze změny
    assert after[1] == ("Fed holds rates", "S&P 500 flat", "keep-1")
    moc = "MOC IMBALANCE S&P 500: 1904 MLN"
    assert after[2] == (moc, None, dedup_hash(moc, TS))
    assert after[3] == ("S&P 500 hits record", None, "keep-3")
    # Kolize: titulek dekódovaný, starý hash zůstává, řádek se nemaže
    assert after[4] == ("Johnson & Johnson Slides", None, "old-4")
    assert after[5] == ROWS[5][1:]
    assert after[6] == ROWS[6][1:]

    assert report.changed == 4
    assert report.titles == {"alpaca": 2, "bluesky": 1}
    assert report.summaries == {"alpaca": 1}
    assert report.rehashed == {"alpaca": 1, "bluesky": 1}
    assert report.collisions == [(5, "alpaca")]
    # Druhý běh nic nemění
    assert script.fix(engine, apply=True).changed == 0
    assert stored(engine) == after


def test_dry_run_nic_nezapise(tmp_path: Path) -> None:
    script = load_script()
    engine = make_engine(tmp_path)
    before = stored(engine)

    report = script.fix(engine, apply=False)

    assert report.changed == 4
    assert len(report.collisions) == 1
    assert stored(engine) == before
    assert "Řádků ke změně (dry-run): 4" in script.format_report(report, apply=False)


def test_dva_radky_nedostanou_tyz_hash(tmp_path: Path) -> None:
    """Tentýž čistý titulek téhož dne z Alpaca i Bluesky — UNIQUE hash dostane jen první."""
    script = load_script()
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'pair.sqlite'}")
    ensure_sentiment_schema(engine)
    with engine.begin() as conn:
        for index, source in enumerate(("alpaca", "bluesky")):
            conn.execute(
                insert(news_events).values(
                    ts_event=TS,
                    ts_ingested=TS,
                    source=source,
                    kind="headline",
                    title="AT&amp;T beats",
                    symbols=[],
                    market_closed=False,
                    dedup_hash=f"old-{index}",
                    raw={},
                )
            )

    report = script.fix(engine, apply=True)

    assert report.rehashed == {"alpaca": 1}
    assert report.collisions == [(2, "bluesky")]
    hashes = [row[2] for row in stored(engine)]
    assert hashes == [dedup_hash("AT&T beats", TS), "old-1"]
