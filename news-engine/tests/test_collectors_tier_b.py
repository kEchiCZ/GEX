"""Testy Tier B (#272): Finnhub, zpravodajské RSS a ochrana klíčů (S10)."""

import datetime as dt
import json

import pytest

from gexlens_news.collectors import CollectorHealth
from gexlens_news.collectors.finnhub import FinnhubCollector
from gexlens_news.collectors.rss import RssCollector
from gexlens_news.http import Response, strip_secrets

TS = dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.UTC)


def clock() -> dt.datetime:
    return TS


class RecordingFetcher:
    """Zaznamenává volané URL — kontrola, že se posílá token i conditional GET."""

    def __init__(self, *responses: Response) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> Response:
        self.calls.append(url)
        return self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]


FINNHUB_PAYLOAD = json.dumps(
    [
        {
            "category": "top news",
            "datetime": 1785240000,
            "headline": "Fed holds rates steady",
            "id": 7654321,
            "related": "",
            "source": "Reuters",
            "summary": "The Federal Reserve kept its benchmark rate unchanged.",
            "url": "https://example.com/a",
        },
        {"datetime": 1785240600, "headline": "", "id": 1},  # bez titulku → přeskočit
        {"datetime": 0, "headline": "Bez času", "id": 2},
    ]
)


# ── Finnhub ────────────────────────────────────────────────────────


async def test_finnhub_normalizes_and_skips_titleless() -> None:
    collector = FinnhubCollector("secret-key", RecordingFetcher(Response(200, FINNHUB_PAYLOAD)))
    items = await collector.fetch()
    assert len(items) == 3

    events = [e for e in (collector.normalize(i) for i in items) if e is not None]
    assert [e.title for e in events] == ["Fed holds rates steady", "Bez času"]

    first = events[0]
    assert first.kind == "headline"
    assert first.source_uid == "7654321"
    assert first.ts_event == dt.datetime.fromtimestamp(1785240000, tz=dt.UTC)
    assert first.summary is not None and "benchmark rate" in first.summary
    # Kategorii a směr doplní klasifikátor v N3 — Tier B je surový vstup
    assert first.category is None
    assert first.importance is None

    # Nulový timestamp = neznámý čas → bereme čas ingestu, event se nezahazuje
    assert events[1].ts_event == items[2].fetched_at


async def test_finnhub_sends_token_but_never_logs_it() -> None:
    """S10: klíč smí být v requestu, ale ne v ničem, co se ukládá."""
    fetcher = RecordingFetcher(Response(200, "[]"))
    collector = FinnhubCollector("super-secret", fetcher, clock=clock)
    await collector.fetch()
    assert "super-secret" in fetcher.calls[0]  # request klíč potřebuje
    assert "super-secret" not in collector.sanitized_url()
    assert "token=***" in collector.sanitized_url()


def test_finnhub_requires_key() -> None:
    with pytest.raises(ValueError, match="API klíč"):
        FinnhubCollector("", RecordingFetcher(Response(200, "[]")))


async def test_finnhub_rejects_unexpected_shape() -> None:
    collector = FinnhubCollector("k", RecordingFetcher(Response(200, '{"error":"limit"}')))
    with pytest.raises(ValueError, match="nevrátil seznam"):
        await collector.fetch()


# ── Ochrana tajemství v chybách (S10) ──────────────────────────────


def test_strip_secrets_masks_query_params() -> None:
    raw = "ConnectError: GET https://finnhub.io/api/v1/news?category=general&token=abc123 failed"
    cleaned = strip_secrets(raw)
    assert "abc123" not in cleaned
    assert "token=***" in cleaned
    assert "category=general" in cleaned  # neutrální parametry zůstávají
    for variant in ("api_key=xyz", "apikey=xyz", "API-KEY=xyz", "secret=xyz"):
        assert "xyz" not in strip_secrets(f"url?{variant}")


def test_health_stores_sanitized_error() -> None:
    """Bez sanitizace by klíč z httpx výjimky skončil v UI přes last_error."""
    health = CollectorHealth(name="finnhub")
    health.record_failure(RuntimeError("GET https://finnhub.io/news?token=leaked-key -> 429"))
    assert health.last_error is not None
    assert "leaked-key" not in health.last_error
    assert "token=***" in health.last_error


# ── Zpravodajské RSS ───────────────────────────────────────────────

CNBC_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Stocks close higher as tech leads</title>
    <link>https://www.cnbc.com/x.html</link>
    <pubDate>Tue, 28 Jul 2026 20:15:00 GMT</pubDate>
    <description>Wall Street ended the session in the green.</description>
    <guid>cnbc-1</guid>
  </item>
</channel></rss>"""


async def test_news_rss_produces_uncategorized_headlines() -> None:
    """Tier B nemá kategorii ani importance — ty určí klasifikátor v N3."""
    collector = RssCollector(
        "rss_news", ["https://cnbc/rss"], RecordingFetcher(Response(200, CNBC_RSS)), clock=clock
    )
    events = [e for e in (collector.normalize(i) for i in await collector.fetch()) if e is not None]
    assert len(events) == 1
    event = events[0]
    assert event.kind == "headline"
    assert event.category is None
    assert event.importance is None
    assert event.source == "rss_news"
    assert event.ts_event == dt.datetime(2026, 7, 28, 20, 15, tzinfo=dt.UTC)


async def test_same_story_from_finnhub_and_rss_shares_dedup_key() -> None:
    """Redundance zdrojů má smysl jen když dedup pozná, že jde o tutéž story."""
    finnhub = FinnhubCollector(
        "k",
        RecordingFetcher(
            Response(
                200,
                json.dumps(
                    [{"datetime": 1785240000, "headline": "Stocks close higher as tech leads"}]
                ),
            )
        ),
        clock=clock,
    )
    rss = RssCollector(
        "rss_news", ["https://cnbc/rss"], RecordingFetcher(Response(200, CNBC_RSS)), clock=clock
    )
    from_finnhub = finnhub.normalize((await finnhub.fetch())[0])
    from_rss = rss.normalize((await rss.fetch())[0])
    assert from_finnhub is not None and from_rss is not None
    assert from_finnhub.dedup_hash == from_rss.dedup_hash
    assert from_finnhub.source != from_rss.source
