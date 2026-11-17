"""Klíč kalendáře s minutou releasu a doplnění zahozených releasů (#1298).

Skript žije ve `scripts/`, načítá se přes importlib jako ostatní skripty.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.compute.newstext import dedup_hash, event_dedup_hash
from gexlens_engine.storage.sentiment import ensure_sentiment_schema, news_events

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_ff_scheduled_1298.py"
NOW = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.UTC)
NFP = "USD Non-Farm Employment Change"
CATCH_UP = dt.datetime(2025, 12, 16, 13, 29, tzinfo=dt.UTC)
CURRENT = dt.datetime(2025, 12, 16, 13, 30, tzinfo=dt.UTC)
CPI = dt.datetime(2026, 10, 14, 12, 30, tzinfo=dt.UTC)
SPEECH_DB = dt.datetime(2026, 10, 15, 14, 0, tzinfo=dt.UTC)
SPEECH_FF = dt.datetime(2026, 10, 15, 15, 0, tzinfo=dt.UTC)
RETAIL = dt.datetime(2026, 10, 16, 12, 30, tzinfo=dt.UTC)


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("backfill_ff_scheduled_1298", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclassy skriptu (odložené anotace) hledají svůj modul v sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _row(
    title: str, ts: dt.datetime, *, kind: str = "scheduled", ff_id: int | None = None
) -> dict[str, Any]:
    raw: dict[str, Any] = {"title": title}
    if ff_id is not None:
        raw = {"id": ff_id, "name": title.removeprefix("USD "), "currency": "USD"}
    return {
        "ts_event": ts,
        "ts_ingested": ts - dt.timedelta(days=3),
        "source": "forexfactory" if kind == "scheduled" else "rss_news",
        "kind": kind,
        "title": title,
        "symbols": [],
        "market_closed": False,
        # Stav před #1298: denní klíč
        "dedup_hash": dedup_hash(title, ts),
        "raw": raw,
    }


def make_db(tmp_path: Path) -> Engine:
    """Produkční tvar před #1298: backfill z 29. 7. s FF id, živý sběr bez id."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(news_events),
            [
                _row(NFP, CATCH_UP, ff_id=151490),
                _row("USD Unemployment Rate", CURRENT, ff_id=142588),
                _row("USD CPI m/m", CPI),
                _row("USD FOMC Member Waller Speaks", SPEECH_DB),
                _row("Fed holds rates", CPI, kind="headline"),
            ],
        )
    return engine


def _entry(
    ff_id: int, name: str, ts: dt.datetime, *, forecast: str = "", actual: str = ""
) -> dict[str, Any]:
    return {
        "id": ff_id,
        "name": name,
        "currency": "USD",
        "dateline": int(ts.timestamp()),
        "impactName": "high",
        "forecast": forecast,
        "previous": "",
        "actual": actual,
    }


PAGES = {
    "dec15.2025": [
        _entry(151490, "Non-Farm Employment Change", CATCH_UP, actual="-105K"),
        _entry(151491, "Non-Farm Employment Change", CURRENT, forecast="50K", actual="64K"),
        _entry(142588, "Unemployment Rate", CURRENT, forecast="4.5%", actual="4.6%"),
    ],
    "oct12.2026": [
        _entry(1, "CPI m/m", CPI, forecast="0.3%", actual="0.3%"),
        _entry(2, "FOMC Member Waller Speaks", SPEECH_FF),
        _entry(3, "Retail Sales m/m", RETAIL, forecast="0.2%", actual="0.4%"),
    ],
}


def fake_fetch(week: str) -> str:
    days = [{"events": PAGES.get(week, [])}]
    return f"<script>window.calendarComponentStates[1] = {{ days: {json.dumps(days)} }};</script>"


def scheduled(engine: Engine) -> list[tuple[str, dt.datetime, str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(news_events.c.title, news_events.c.ts_event, news_events.c.dedup_hash)
            .where(news_events.c.kind == "scheduled")
            .order_by(news_events.c.ts_event, news_events.c.id)
        ).fetchall()
    return [(r.title, r.ts_event.replace(tzinfo=dt.UTC), r.dedup_hash) for r in rows]


def test_history_weeks_start_one_week_early_and_end_this_week() -> None:
    script = load_script()
    weeks = script.history_weeks(dt.date(2025, 12, 16), dt.date(2026, 1, 6))
    assert weeks == ["dec8.2025", "dec15.2025", "dec22.2025", "dec29.2025", "jan5.2026"]


def test_dry_run_plans_missing_releases_and_writes_nothing(tmp_path: Path) -> None:
    script = load_script()
    engine = make_db(tmp_path)
    before = scheduled(engine)

    rehashed = script.rehash(engine, apply=False)
    filled = script.backfill(engine, apply=False, fetch=fake_fetch, now=NOW, log=lambda _l: None)

    assert (rehashed.rows, rehashed.changed) == (4, 4)
    # Druhý NFP 16. 12. a Retail Sales, které v DB nejsou vůbec
    assert [(e.title, e.ts_event) for e in filled.missing] == [
        (NFP, CURRENT),
        ("USD Retail Sales m/m", RETAIL),
    ]
    # Projev posunutý FF ze 14:00 na 15:00: týž release, nezapisuje se
    [(row, event)] = filled.rescheduled
    assert (row.ts_event, event.ts_event) == (SPEECH_DB, SPEECH_FF)
    assert filled.unclear == []
    assert scheduled(engine) == before


def test_apply_rehashes_fills_and_is_idempotent(tmp_path: Path) -> None:
    script = load_script()
    engine = make_db(tmp_path)

    assert script.rehash(engine, apply=True).changed == 4
    filled = script.backfill(engine, apply=True, fetch=fake_fetch, now=NOW, log=lambda _l: None)

    assert filled.written == 2
    rows = scheduled(engine)
    assert sorted((ts, title) for title, ts, _ in rows) == [
        (ts, title)
        for title, ts in [
            (NFP, CATCH_UP),
            (NFP, CURRENT),
            ("USD Unemployment Rate", CURRENT),
            ("USD CPI m/m", CPI),
            ("USD FOMC Member Waller Speaks", SPEECH_DB),
            ("USD Retail Sales m/m", RETAIL),
        ]
    ]
    assert all(h == event_dedup_hash("scheduled", t, ts) for t, ts, h in rows)
    with engine.connect() as conn:
        headline = conn.execute(
            select(news_events.c.dedup_hash).where(news_events.c.kind == "headline")
        ).scalar_one()
        current_actual = conn.execute(
            select(news_events.c.actual).where(
                news_events.c.title == NFP, news_events.c.forecast.is_not(None)
            )
        ).scalar_one()
    assert headline == dedup_hash("Fed holds rates", CPI)
    assert float(current_actual) == pytest.approx(64_000)

    assert script.rehash(engine, apply=True).changed == 0
    again = script.backfill(engine, apply=True, fetch=fake_fetch, now=NOW, log=lambda _l: None)
    assert (again.missing, again.written) == ([], 0)


def test_revert_restores_day_key_for_first_release_of_the_day(tmp_path: Path) -> None:
    """Návrat na starší news-engine: první řádek dne denní klíč, doplněný druhý s minutou."""
    script = load_script()
    engine = make_db(tmp_path)
    script.rehash(engine, apply=True)
    script.backfill(engine, apply=True, fetch=fake_fetch, now=NOW, log=lambda _l: None)

    reverted = script.rehash(engine, apply=True, revert=True)

    assert reverted.reverted_kept == 1
    hashes = {(t, ts): h for t, ts, h in scheduled(engine)}
    assert hashes[(NFP, CATCH_UP)] == dedup_hash(NFP, CATCH_UP)
    assert hashes[(NFP, CURRENT)] == event_dedup_hash("scheduled", NFP, CURRENT)
    assert hashes[("USD CPI m/m", CPI)] == dedup_hash("USD CPI m/m", CPI)


def test_cached_fetch_downloads_each_week_once(tmp_path: Path) -> None:
    script = load_script()
    calls: list[str] = []
    pauses: list[float] = []

    def fetch(week: str) -> str:
        calls.append(week)
        return f"page {week}"

    get = script.cached_fetch(tmp_path, fetch, throttle_s=2.0, sleep=pauses.append)
    assert get("dec15.2025") == get("dec15.2025") == "page dec15.2025"
    assert (calls, pauses) == (["dec15.2025"], [2.0])
