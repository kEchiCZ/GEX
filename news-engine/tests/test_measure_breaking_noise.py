"""Šum karty breaking news — potvrzené × nepotvrzené (ADR-0059 bod 4, #1491).

Skript žije ve `scripts/`, načítá se přes importlib jako ostatní skripty.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, insert

from gexlens_engine.compute.news_tier import SourceCopy
from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    news_event_sources,
    news_events,
)

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "measure_breaking_noise.py"
T0 = dt.datetime(2026, 10, 9, 13, 0, tzinfo=dt.UTC)
MIN = dt.timedelta(minutes=1)


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("measure_breaking_noise", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclassy skriptu (odložené anotace) hledají svůj modul v sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = load_script()
UNTIL = T0 + dt.timedelta(hours=2)


def event(
    tier: int | None,
    *,
    importance: int = 2,
    category: str = "FED",
    source: str = "rss_news",
    kind: str = "headline",
    ingested: dt.datetime = T0,
    lag: dt.timedelta = dt.timedelta(0),
) -> Any:
    return script.Event(1, ingested - lag, ingested, source, kind, category, importance, tier)


def copy(source: str, tier: int | None, fetched: dt.datetime) -> Any:
    return script.Copy(source, SourceCopy(tier, fetched, fetched))


def test_tier_2_is_confirmed_on_entry() -> None:
    item = script.timeline(event(2, source="alpaca"), [], until=UNTIL)
    assert item.entered == T0
    assert item.confirmed_on_entry
    assert item.confirmed_by == "alpaca"
    assert item.lead is None


def test_tier_3_confirmed_later_by_copy() -> None:
    copies = [copy("finnhub", 3, T0 + 2 * MIN), copy("alpaca", 2, T0 + 7 * MIN)]
    item = script.timeline(event(3), copies, until=UNTIL)
    assert item.entered == T0
    assert not item.confirmed_on_entry
    assert item.confirmed == T0 + 7 * MIN
    assert item.confirmed_by == "alpaca"
    assert item.lead == 7 * MIN


def test_copy_after_until_does_not_confirm() -> None:
    item = script.timeline(event(3), [copy("alpaca", 2, UNTIL)], until=UNTIL)
    assert item.confirmed is None
    assert item.lead is None


def test_copy_before_first_delivery_counts_from_arrival() -> None:
    item = script.timeline(event(3), [copy("alpaca", 2, T0 - MIN)], until=UNTIL)
    assert item.entered == T0
    assert item.confirmed_on_entry
    assert item.confirmed_by == "alpaca"


def test_entry_through_copy_when_first_delivery_has_no_tier() -> None:
    copies = [copy("rss_news", 3, T0 + 3 * MIN), copy("fed_rss", 1, T0 + 9 * MIN)]
    item = script.timeline(event(None, source="bluesky"), copies, until=UNTIL)
    assert item.entered == T0 + 3 * MIN
    assert item.lead == 6 * MIN
    assert item.confirmed_by == "fed_rss"


def test_not_breaking_returns_none() -> None:
    assert script.timeline(event(2, importance=1), [], until=UNTIL) is None
    assert script.timeline(event(None), [], until=UNTIL) is None
    assert script.timeline(event(2, category="EARNINGS"), [], until=UNTIL) is None


def test_daily_and_by_source_counts() -> None:
    items = [
        script.timeline(event(2, source="alpaca"), [], until=UNTIL),
        script.timeline(event(3, importance=3), [copy("alpaca", 2, T0 + MIN)], until=UNTIL),
        script.timeline(event(3, source="finnhub", lag=dt.timedelta(hours=8)), [], until=UNTIL),
    ]
    stats = script.daily(items)[T0.date()]
    assert (stats.total, stats.confirmed_on_entry, stats.unconfirmed) == (3, 1, 2)
    assert stats.later_confirmed == 1
    assert stats.unconfirmed_key == 1  # importance 3 = zásadní
    sources = script.by_source(items)
    assert set(sources) == {"rss_news", "finnhub"}
    assert sources["finnhub"].lags_min == [480.0]
    assert sources["rss_news"].later_confirmed == 1
    report = script.render(items, as_of=UNTIL, days=1)
    assert "medián náskoku karty: 1.0 min (n = 1)" in report


def test_load_from_db(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    rows = [
        # (zdroj, tier, importance, kategorie, příjem) → kandidát?
        ("rss_news", 3, 2, "FED", T0),
        ("alpaca", 2, 1, "FED", T0),  # mimo předfiltr importance
        ("alpaca", 2, 3, "GEOPOLITICS", T0 - dt.timedelta(days=3)),  # mimo okno
    ]
    with engine.begin() as conn:
        ids = [
            conn.execute(
                insert(news_events)
                .values(
                    ts_event=ingested,
                    ts_ingested=ingested,
                    source=source,
                    kind="headline",
                    category=category,
                    importance=importance,
                    content_tier=tier,
                    title=f"zpráva {index}",
                    symbols=[],
                    market_closed=False,
                    dedup_hash=f"h{index}",
                    raw={},
                )
                .returning(news_events.c.id)
            ).scalar_one()
            for index, (source, tier, importance, category, ingested) in enumerate(rows)
        ]
        conn.execute(
            insert(news_event_sources).values(
                event_id=ids[0],
                source="alpaca",
                source_uid="a1",
                content_tier=2,
                published_at=T0,
                fetched_at=T0 + 5 * MIN,
            )
        )
    items = script.load(engine, T0 - dt.timedelta(days=1), UNTIL)
    assert [item.event.id for item in items] == [ids[0]]
    assert items[0].lead == 5 * MIN
    assert items[0].confirmed_by == "alpaca"
