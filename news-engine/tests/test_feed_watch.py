"""Selhání jednotlivých RSS feedů a jejich hlídka (#1451).

Yahoo `rssindex` vracel od 23. 9. 2026 HTTP 404 dva týdny bez upozornění:
zdroj `rss_news` byl „ok", protože CNBC a MarketWatch odpovídaly.
"""

import datetime as dt

import httpx
import pytest

from gexlens_news.collectors.rss import FeedFailure, RssCollector
from gexlens_news.config import NEWS_RSS_URLS
from gexlens_news.feed_watch import ERROR_KIND, RECOVERED_KIND, FeedWatch, feed_label
from gexlens_news.http import Response

DEAD = "https://finance.yahoo.com/news/rssindex"
ALIVE = "https://www.cnbc.com/id/100003114/device/rss/rss.html"
FEED = (
    "<rss><channel><item><title>Fed holds rates</title><link>https://x/a</link>"
    "<pubDate>Wed, 07 Oct 2026 20:00:00 +0000</pubDate></item></channel></rss>"
)
# Středa 7. 10. 2026 20:00 UTC; sobota 10. 10. 2026
WEDNESDAY = dt.datetime(2026, 10, 7, 20, 0, tzinfo=dt.UTC)
SATURDAY = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.UTC)


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


class Fetcher:
    """Mrtvé URL vrací daný HTTP status, ostatní `responses[url]` (výchozí FEED)."""

    def __init__(self, dead: dict[str, int], responses: dict[str, Response] | None = None) -> None:
        self.dead = dead
        self.responses = responses or {}

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> Response:
        if url in self.dead:
            status = self.dead[url]
            raise httpx.HTTPStatusError(
                str(status), request=httpx.Request("GET", url), response=httpx.Response(status)
            )
        return self.responses.get(url, Response(status=200, text=FEED))


def _collector(fetcher: Fetcher, clock: Clock) -> RssCollector:
    return RssCollector("rss_news", [ALIVE, DEAD], fetcher, clock=clock)


# ── Collector: stav selhání po feedech ─────────────────────────────


async def test_selhani_feedu_drzi_cas_prvniho_selhani_a_po_uspechu_zmizi() -> None:
    clock = Clock(WEDNESDAY)
    fetcher = Fetcher({DEAD: 404})
    collector = _collector(fetcher, clock)

    items = await collector.fetch()  # živý feed dodá, mrtvý se jen zapíše
    assert len(items) == 1
    assert collector.feed_failures[DEAD].since == WEDNESDAY
    assert collector.feed_failures[DEAD].last_error == "HTTP 404"
    assert ALIVE not in collector.feed_failures

    clock.now = WEDNESDAY + dt.timedelta(minutes=30)
    await collector.fetch()
    assert collector.feed_failures[DEAD].since == WEDNESDAY  # řada pokračuje, čas se nemění

    fetcher.dead.clear()
    await collector.fetch()
    assert collector.feed_failures == {}


async def test_mrtvy_feed_a_304_u_ostatnich_nevyhodi_chybu() -> None:
    """Dřív „chyba a žádné položky" = výjimka → backoff zpomalil i zdravé feedy."""
    fetcher = Fetcher({DEAD: 404}, {ALIVE: Response(status=304, text="", not_modified=True)})
    collector = _collector(fetcher, Clock(WEDNESDAY))
    assert await collector.fetch() == []
    assert DEAD in collector.feed_failures


async def test_selhani_vsech_feedu_vyhodi_chybu_pro_runner() -> None:
    fetcher = Fetcher({DEAD: 404, ALIVE: 503})
    collector = _collector(fetcher, Clock(WEDNESDAY))
    with pytest.raises(RuntimeError, match="HTTP 404"):
        await collector.fetch()
    assert set(collector.feed_failures) == {ALIVE, DEAD}


async def test_304_je_uspech_feedu() -> None:
    clock = Clock(WEDNESDAY)
    fetcher = Fetcher({DEAD: 404})
    collector = _collector(fetcher, clock)
    await collector.fetch()
    fetcher.dead.clear()
    fetcher.responses[DEAD] = Response(status=304, text="", not_modified=True)
    await collector.fetch()
    assert collector.feed_failures == {}


def test_yahoo_rssindex_v_konfiguraci_neni() -> None:
    """Rozhodnutí 8. 10. 2026: headline feedy ^GSPC a ^IXIC místo zrušeného rssindex."""
    assert DEAD not in NEWS_RSS_URLS
    yahoo = [url for url in NEWS_RSS_URLS if "yahoo.com" in url]
    assert len(yahoo) == 2
    assert any("s=%5EGSPC" in url for url in yahoo)
    assert any("s=%5EIXIC" in url for url in yahoo)


# ── Hlídka: hranové alerty ─────────────────────────────────────────


async def test_hlidka_ohlasi_feed_po_hodine_jednou_a_po_obnove_jednou() -> None:
    clock = Clock(WEDNESDAY)
    fetcher = Fetcher({DEAD: 404})
    collector = _collector(fetcher, clock)
    watch = FeedWatch([collector])

    await collector.fetch()
    clock.now = WEDNESDAY + dt.timedelta(minutes=59)
    await collector.fetch()
    assert watch.run(clock.now) == []  # krátký výpadek se nehlásí

    clock.now = WEDNESDAY + dt.timedelta(hours=1)
    await collector.fetch()
    [alert] = watch.run(clock.now)
    assert alert["kind"] == ERROR_KIND
    assert alert["source"] == "rss_news" and alert["feed"] == DEAD
    assert "HTTP 404" in alert["message"] and "1 h" in alert["message"]
    assert alert["ts"] == int(clock.now.timestamp())

    clock.now = WEDNESDAY + dt.timedelta(days=3)
    await collector.fetch()
    assert watch.run(clock.now) == []  # trvající výpadek už nespamuje

    fetcher.dead.clear()
    await collector.fetch()
    [recovered] = watch.run(clock.now)
    assert recovered["kind"] == RECOVERED_KIND and recovered["feed"] == DEAD
    assert "3 d" in recovered["message"]
    assert watch.run(clock.now) == []


async def test_hlidka_hlasi_i_o_vikendu() -> None:
    """RSS publikuje i o víkendu — data se očekávají vždy, chyba HTTP není ticho trhu."""
    clock = Clock(SATURDAY)
    collector = _collector(Fetcher({DEAD: 404}), clock)
    watch = FeedWatch([collector])
    await collector.fetch()
    clock.now = SATURDAY + dt.timedelta(hours=2)
    await collector.fetch()
    assert [alert["kind"] for alert in watch.run(clock.now)] == [ERROR_KIND]


async def test_hlidka_obnova_bez_predchoziho_alertu_mlci() -> None:
    clock = Clock(WEDNESDAY)
    fetcher = Fetcher({DEAD: 404})
    collector = _collector(fetcher, clock)
    watch = FeedWatch([collector])
    await collector.fetch()
    fetcher.dead.clear()
    clock.now = WEDNESDAY + dt.timedelta(minutes=10)
    await collector.fetch()
    assert watch.run(clock.now) == []


async def test_hlidka_drzi_feedy_ruznych_zdroju_zvlast() -> None:
    clock = Clock(WEDNESDAY)
    other = "https://www.reddit.com/r/stocks/hot/.rss"
    news = _collector(Fetcher({DEAD: 404}), clock)
    reddit = RssCollector("reddit_rss", [other], Fetcher({other: 403}), clock=clock)
    watch = FeedWatch([news, reddit])
    await news.fetch()
    with pytest.raises(RuntimeError):
        await reddit.fetch()
    clock.now = WEDNESDAY + dt.timedelta(hours=1)
    alerts = watch.run(clock.now)
    assert {(alert["source"], alert["feed"]) for alert in alerts} == {
        ("rss_news", DEAD),
        ("reddit_rss", other),
    }


async def test_necitelne_xml_je_selhani_feedu_a_rada_pokracuje() -> None:
    """Feed, který místo RSS vrací stránku s chybou, se počítá jako selhávající."""
    clock = Clock(WEDNESDAY)
    broken = Response(status=200, text="<html><body>Not found")
    collector = _collector(Fetcher({}, {DEAD: broken}), clock)
    await collector.fetch()
    clock.now = WEDNESDAY + dt.timedelta(hours=2)
    await collector.fetch()
    failure = collector.feed_failures[DEAD]
    assert failure.since == WEDNESDAY and failure.last_error == "ParseError"


def test_zpravy_se_lisi_v_prvnich_80_znacich() -> None:
    """Telegram slučuje alerty se shodným začátkem zprávy — feedy se tam musí lišit."""
    from gexlens_news.config import FED_RSS_URLS

    now = WEDNESDAY + dt.timedelta(hours=2)
    failure = FeedFailure(since=WEDNESDAY, last_error="HTTP 404")
    for urls in (NEWS_RSS_URLS, FED_RSS_URLS):
        heads = {FeedWatch._failed(("rss_news", url), failure, now)["message"][:80] for url in urls}
        assert len(heads) == len(urls)
    assert feed_label(NEWS_RSS_URLS[2]).endswith(" ^GSPC")


async def test_feed_ozil_a_znovu_selhal_mezi_tiky_je_nova_epizoda() -> None:
    clock = Clock(WEDNESDAY)
    fetcher = Fetcher({DEAD: 404})
    collector = _collector(fetcher, clock)
    watch = FeedWatch([collector])
    await collector.fetch()
    clock.now = WEDNESDAY + dt.timedelta(hours=1)
    assert [a["kind"] for a in watch.run(clock.now)] == [ERROR_KIND]

    fetcher.dead.clear()
    await collector.fetch()  # ožil…
    fetcher.dead[DEAD] = 500
    clock.now = WEDNESDAY + dt.timedelta(hours=1, minutes=1)
    await collector.fetch()  # …a před dalším tikem hlídky zase selhal
    assert [a["kind"] for a in watch.run(clock.now)] == [RECOVERED_KIND]
    clock.now = WEDNESDAY + dt.timedelta(hours=2, minutes=1)
    [again] = watch.run(clock.now)
    assert again["kind"] == ERROR_KIND and "HTTP 500" in again["message"]


async def test_round_robin_drzi_selhani_nestazeneho_feedu() -> None:
    """Reddit stahuje jeden feed za cyklus — úspěch druhého selhání prvního nesmaže."""
    clock = Clock(WEDNESDAY)
    first, second = "https://www.reddit.com/r/a/hot/.rss", "https://www.reddit.com/r/b/hot/.rss"
    collector = RssCollector(
        "reddit_rss", [first, second], Fetcher({first: 429}), round_robin=True, clock=clock
    )
    with pytest.raises(RuntimeError, match="HTTP 429"):  # jediný splatný feed selhal
        await collector.fetch()
    await collector.fetch()  # druhý feed uspěje
    assert set(collector.feed_failures) == {first}
