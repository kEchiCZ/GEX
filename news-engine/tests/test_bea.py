"""BEA releasy z RSS (E-6.25d, #1517, ADR-0059 bod 5).

Feed drží jen nejnovější vydání každé řady; `pubDate` je čas releasu, takže
`ts_event` = čas položky (`raw.ts_source = item`). Kategorie a importance
podle řady v titulku (GDP a PCE 3), ostatní vydání 1 (vlastník 11. 10. 2026),
směr 0.
"""

import datetime as dt

import pytest

from gexlens_news.classifier import classify, feed_of
from gexlens_news.collectors.rss import RssCollector
from gexlens_news.config import BEA_RSS_URLS
from gexlens_news.http import Response
from gexlens_news.model import NewsEvent

#: Po stažení snímku z 11. 10. 2026 (všechny položky jsou v minulosti)
NOW = dt.datetime(2026, 10, 11, 9, 0, tzinfo=dt.UTC)


def _item(slug: str, title: str, pub_date: str) -> str:
    link = f"http://www.bea.gov/news/2026/{slug}"
    return (
        f"<item><title>{title}</title><link>{link}</link>"
        f"<pubDate>{pub_date}</pubDate><guid>{link}</guid></item>"
    )


def _feed(*items: str) -> str:
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>News Releases</title>'
        + "".join(items)
        + "</channel></rss>"
    )


class Fetcher:
    def __init__(self, body: str) -> None:
        self._body = body

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> Response:
        return Response(200, self._body)


async def _events(body: str) -> list[NewsEvent]:
    collector = RssCollector(
        "bea", BEA_RSS_URLS, Fetcher(body), verified_time=True, clock=lambda: NOW
    )
    items = await collector.fetch()
    return [e for e in (collector.normalize(i) for i in items) if e is not None]


@pytest.mark.parametrize(
    ("pub_date", "expected"),
    [
        # 8:30 EDT a 8:30 EST — feed nese posun ET, ne UTC
        ("Tue, 06 Oct 2026 08:30:00 -0400", dt.datetime(2026, 10, 6, 12, 30, tzinfo=dt.UTC)),
        ("Thu, 05 Feb 2026 08:30:00 -0500", dt.datetime(2026, 2, 5, 13, 30, tzinfo=dt.UTC)),
        # Roční vydání v 10:00 ET
        ("Thu, 05 Mar 2026 10:00:00 -0500", dt.datetime(2026, 3, 5, 15, 0, tzinfo=dt.UTC)),
    ],
)
async def test_cas_releasu_je_cas_polozky(pub_date: str, expected: dt.datetime) -> None:
    (event,) = await _events(_feed(_item("release", "Release", pub_date)))
    assert event.ts_event == expected
    assert event.raw["ts_source"] == "item"


async def test_prvni_stazeni_zapise_snimek_feedu() -> None:
    """Nejnovější vydání každé řady — starší releasy téže řady ve feedu nejsou."""
    body = _feed(
        _item(
            "trade-august-2026",
            "U.S. International Trade in Goods and Services, August 2026",
            "Tue, 06 Oct 2026 08:30:00 -0400",
        ),
        _item(
            "pio-august-2026",
            "Personal Income and Outlays, August 2026",
            "Wed, 30 Sep 2026 08:30:00 -0400",
        ),
    )
    events = await _events(body)
    assert [e.title for e in events] == [
        "U.S. International Trade in Goods and Services, August 2026",
        "Personal Income and Outlays, August 2026",
    ]
    assert {e.source for e in events} == {"bea"}


@pytest.mark.parametrize(
    ("title", "category", "importance"),
    [
        # Titulky ze snímku feedu 11. 10. 2026 a z rozvrhu BEA
        ("GDP (Advance Estimate), 3rd Quarter 2026", "MACRO_GROWTH", 3),
        (
            "GDP (Third Estimate), Industries, Corporate Profits, State GDP, and State Personal "
            "Income, 2nd Quarter 2026",
            "MACRO_GROWTH",
            3,
        ),
        ("Personal Income and Outlays, August 2026", "MACRO_INFLATION", 3),
        # Ostatní vydání: importance 1 (vlastník 11. 10.), kategorie z titulku —
        # obchod a běžný účet hodnotí kalendář FF Low impact
        ("U.S. International Trade in Goods and Services, August 2026", "OTHER", 1),
        (
            "U.S. International Transactions and Investment Position, 2nd Quarter 2026",
            "OTHER",
            1,
        ),
        ("Gross Domestic Product by County and Personal Income by County, 2024", "OTHER", 1),
        (
            "Real Personal Consumption Expenditures by State and Real Personal Income by "
            "State, 2024",
            "OTHER",
            1,
        ),
        ("Outdoor Recreation Economic Statistics, U.S. and States, 2024", "OTHER", 1),
        ("Direct Investment by Country and Industry, 2025", "OTHER", 1),
        ("New Foreign Direct Investment in the United States, 2025", "OTHER", 1),
    ],
)
def test_kategorie_a_importance_podle_rady(title: str, category: str, importance: int) -> None:
    feed = feed_of("bea", {"feed": BEA_RSS_URLS[0]})
    result = classify(title, feed=feed, kind="headline")
    assert (result.category, result.importance) == (category, importance)
    # Směr nese řádek kalendáře jako u BLS
    assert (result.direction, result.strength) == (0, 0.0)
