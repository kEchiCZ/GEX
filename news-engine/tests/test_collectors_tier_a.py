"""Testy Tier A collectorů (#271): ForexFactory kalendář a Fed RSS."""

import datetime as dt
import json
from pathlib import Path

import pytest

from gexlens_news.collectors.forexfactory import (
    ForexFactoryCollector,
    classify_title,
    parse_number,
)
from gexlens_news.collectors.rss import RssCollector, parse_feed_time, parse_items
from gexlens_news.http import Response

TS = dt.datetime(2026, 7, 27, 12, 0, tzinfo=dt.UTC)
FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "Sentiment"
    / "fixtures"
    / "forexfactory"
    / "ff_calendar_thisweek_2026-07-27.json"
)


class FakeFetcher:
    """Vrací připravené odpovědi; umí 304 i výjimku."""

    def __init__(self, *responses: Response | Exception) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> Response:
        self.calls.append(url)
        item = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def clock() -> dt.datetime:
    return TS


# ── ForexFactory ───────────────────────────────────────────────────


def test_parse_number_handles_calendar_formats() -> None:
    assert parse_number("3.4%") == pytest.approx(3.4)
    assert parse_number("-1.2K") == pytest.approx(-1200.0)
    assert parse_number("86.1") == pytest.approx(86.1)
    assert parse_number("1,250") == pytest.approx(1250.0)
    # Prázdné a nečitelné hodnoty nesmí shodit normalizaci
    for empty in ("", "  ", "-", None, "n/a"):
        assert parse_number(empty) is None


def test_classify_title_prefers_specific_patterns() -> None:
    assert classify_title("FOMC Statement") == "FED"
    assert classify_title("Federal Funds Rate") == "FED"
    assert classify_title("Core PCE Price Index m/m") == "MACRO_INFLATION"
    assert classify_title("Non-Farm Employment Change") == "MACRO_LABOR"
    assert classify_title("Advance GDP q/q") == "MACRO_GROWTH"
    assert classify_title("Crude Oil Inventories") == "ENERGY"
    assert classify_title("German ifo Business Climate") == "OTHER"


async def test_forexfactory_normalizes_real_fixture() -> None:
    """Nad zachyceným payloadem z 27. 7. (ADR-0013) — 90 eventů."""
    collector = ForexFactoryCollector(
        FakeFetcher(Response(200, FIXTURE.read_text(encoding="utf-8"))), clock=clock
    )
    items = await collector.fetch()
    assert len(items) == 90

    events = [e for e in (collector.normalize(i) for i in items) if e is not None]
    assert len(events) == 90
    assert all(e.kind == "scheduled" for e in events)
    assert all(e.ts_event.tzinfo is dt.UTC for e in events)
    # Feed actual nenese (ADR-0013) — doplní oficiální API v N2
    assert all(e.actual is None for e in events)

    fomc = next(e for e in events if "FOMC Statement" in e.title)
    assert fomc.category == "FED"
    assert fomc.importance == 3
    assert fomc.symbols == ["ES", "NQ"]
    # 2026-07-29T14:00:00-04:00 → 18:00 UTC
    assert fomc.ts_event == dt.datetime(2026, 7, 29, 18, 0, tzinfo=dt.UTC)

    gdp = next(e for e in events if "Advance GDP" in e.title)
    assert gdp.forecast == pytest.approx(2.3)
    assert gdp.previous == pytest.approx(2.0)

    # Ne-USD event se sbírá, ale symboly nedostane (reakční okno by měřilo šum)
    eur = next(e for e in events if e.raw.get("country") == "EUR")
    assert eur.symbols == []


async def test_forexfactory_recurring_events_do_not_collide() -> None:
    """Měsíčně opakovaný release musí mít jiný dedup klíč než minulý."""
    payload = json.dumps(
        [
            {
                "title": "Core PCE Price Index m/m",
                "country": "USD",
                "date": "2026-07-30T08:30:00-04:00",
                "impact": "High",
                "forecast": "0.1%",
                "previous": "0.3%",
            },
            {
                "title": "Core PCE Price Index m/m",
                "country": "USD",
                "date": "2026-08-28T08:30:00-04:00",
                "impact": "High",
                "forecast": "0.2%",
                "previous": "0.1%",
            },
        ]
    )
    collector = ForexFactoryCollector(FakeFetcher(Response(200, payload)), clock=clock)
    events = [collector.normalize(i) for i in await collector.fetch()]
    july, august = events[0], events[1]
    assert july is not None and august is not None
    assert july.dedup_hash != august.dedup_hash
    assert july.source_uid != august.source_uid


async def test_forexfactory_skips_broken_entries_and_304() -> None:
    payload = json.dumps(
        [
            {"title": "Bez data", "country": "USD", "impact": "High"},
            {"title": "", "country": "USD", "date": "2026-07-30T08:30:00-04:00"},
            {"title": "Špatné datum", "country": "USD", "date": "vcera"},
            {"title": "OK", "country": "USD", "date": "2026-07-30T08:30:00-04:00"},
        ]
    )
    collector = ForexFactoryCollector(FakeFetcher(Response(200, payload)), clock=clock)
    events = [e for e in (collector.normalize(i) for i in await collector.fetch()) if e is not None]
    assert [e.raw["title"] for e in events] == ["OK"]

    # 304 = feed se nezměnil, žádná práce
    unchanged = ForexFactoryCollector(FakeFetcher(Response(304, "", not_modified=True)))
    assert await unchanged.fetch() == []


async def test_forexfactory_rejects_unexpected_shape() -> None:
    """Formát není garantovaný — nesmysl musí vyhodit, ať se zdroj degraduje."""
    collector = ForexFactoryCollector(FakeFetcher(Response(200, '{"error": "nope"}')))
    with pytest.raises(ValueError, match="tvar seznamu"):
        await collector.fetch()


# ── RSS (Fed a obecný parser) ──────────────────────────────────────

FED_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>FRB: Press Releases</title>
  <item>
    <title>Federal Reserve issues FOMC statement</title>
    <link>https://www.federalreserve.gov/a.htm</link>
    <pubDate>Wed, 29 Jul 2026 18:00:00 GMT</pubDate>
    <description>The Committee decided to maintain the target range.</description>
    <guid>fed-2026-07-29</guid>
  </item>
  <item>
    <title>Speech by Chair Powell</title>
    <link>https://www.federalreserve.gov/b.htm</link>
    <pubDate>Thu, 30 Jul 2026 13:30:00 GMT</pubDate>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Atom headline</title>
    <link href="https://example.com/x"/>
    <published>2026-07-28T10:15:00Z</published>
    <summary>Shrnutí</summary>
    <id>atom-1</id>
  </entry>
</feed>"""


def test_parse_feed_time_accepts_rfc822_and_iso() -> None:
    assert parse_feed_time("Wed, 29 Jul 2026 18:00:00 GMT") == dt.datetime(
        2026, 7, 29, 18, 0, tzinfo=dt.UTC
    )
    assert parse_feed_time("2026-07-28T10:15:00+00:00") == dt.datetime(
        2026, 7, 28, 10, 15, tzinfo=dt.UTC
    )
    # Naivní čas bereme jako UTC, ne jako lokální
    assert parse_feed_time("2026-07-28T10:15:00") == dt.datetime(2026, 7, 28, 10, 15, tzinfo=dt.UTC)
    assert parse_feed_time(None) is None
    assert parse_feed_time("nesmysl") is None


def test_parse_items_handles_rss_and_atom() -> None:
    rss = parse_items(FED_RSS)
    assert [i["title"] for i in rss] == [
        "Federal Reserve issues FOMC statement",
        "Speech by Chair Powell",
    ]
    atom = parse_items(ATOM)
    assert atom[0]["title"] == "Atom headline"
    assert atom[0]["link"] == "https://example.com/x"  # href atribut, ne text


async def test_fed_rss_collector_normalizes() -> None:
    collector = RssCollector(
        "fed_rss",
        ["https://www.federalreserve.gov/feeds/press_all.xml"],
        FakeFetcher(Response(200, FED_RSS)),
        interval_s=300.0,
        category="FED",
        importance=3,
        symbols=["ES", "NQ"],
        clock=clock,
    )
    events = [e for e in (collector.normalize(i) for i in await collector.fetch()) if e is not None]
    assert len(events) == 2
    first = events[0]
    assert first.category == "FED"
    assert first.importance == 3
    assert first.source_uid == "fed-2026-07-29"
    assert first.ts_event == dt.datetime(2026, 7, 29, 18, 0, tzinfo=dt.UTC)
    assert first.summary is not None and "maintain the target range" in first.summary
    # Položka bez description nemá summary, ale event vzniká
    assert events[1].summary is None


async def test_rss_collector_survives_partial_feed_failure() -> None:
    """Jeden mrtvý feed nesmí zahodit zprávy z ostatních."""

    class MixedFetcher:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get(self, url: str, *, headers: dict[str, str] | None = None) -> Response:
            self.calls.append(url)
            if "dead" in url:
                raise TimeoutError("neodpovídá")
            return Response(200, FED_RSS)

    collector = RssCollector(
        "mixed", ["https://dead.example/rss", "https://ok.example/rss"], MixedFetcher(), clock=clock
    )
    items = await collector.fetch()
    assert len(items) == 2  # ze živého feedu

    # Když padnou všechny, výjimka projde do runneru → degradace zdroje
    class DeadFetcher:
        async def get(self, url: str, *, headers: dict[str, str] | None = None) -> Response:
            raise TimeoutError("neodpovídá")

    dead = RssCollector("dead", ["https://a/rss", "https://b/rss"], DeadFetcher(), clock=clock)
    with pytest.raises(RuntimeError):
        await dead.fetch()


async def test_rss_uses_conditional_get_and_skips_unchanged() -> None:
    fetcher = FakeFetcher(Response(200, FED_RSS), Response(304, "", not_modified=True))
    collector = RssCollector("fed_rss", ["https://x/rss"], fetcher, clock=clock)
    assert len(await collector.fetch()) == 2
    assert await collector.fetch() == []


async def test_summary_is_truncated() -> None:
    long_text = "x" * 900
    xml = (
        '<?xml version="1.0"?><rss version="2.0"><channel><item>'
        "<title>T</title><description>" + long_text + "</description>"
        "<pubDate>Wed, 29 Jul 2026 18:00:00 GMT</pubDate></item></channel></rss>"
    )

    class OneShot:
        async def get(self, url: str, *, headers: dict[str, str] | None = None) -> Response:
            return Response(200, xml)

    collector = RssCollector("rss", ["https://x/rss"], OneShot(), clock=clock)
    items = await collector.fetch()
    event = collector.normalize(items[0])
    assert event is not None and event.summary is not None
    assert len(event.summary) == 500
