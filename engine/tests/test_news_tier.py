"""Tier obsahu zprávy — mapování ADR-0059 bod 1 a pokrytí registru zdrojů (#1486)."""

import datetime as dt

import pytest

from gexlens_engine.compute.news_tier import (
    ARTICLE,
    FIXED_TIERS,
    HEADLINE,
    IBKR_PREFIX,
    OFFICIAL,
    PAYLOAD_SOURCES,
    SourceCopy,
    content_tier,
    earliest_published,
    effective_tier,
)
from gexlens_engine.storage.sentiment import NEWS_SOURCE_SEED


@pytest.mark.parametrize(
    ("source", "raw", "tier"),
    [
        # Tier 1: úřad sám
        ("fed_rss", {"feed": "https://www.federalreserve.gov/feeds/speeches.xml"}, OFFICIAL),
        # Tier 2: Alpaca jen s autorem Newsdesk (headliny), Bluesky jen s kurátorem
        ("alpaca", {"author": "Benzinga Newsdesk"}, HEADLINE),
        ("bluesky", {"did": "did:plc:x", "curated": True}, HEADLINE),
        # Tier 3: články a analýzy
        ("alpaca", {"author": "Benzinga Insights"}, ARTICLE),
        ("alpaca", {"author": "Jane Doe"}, ARTICLE),
        ("alpaca", {}, ARTICLE),  # autor chybí — headline se nepředpokládá
        ("alpaca", None, ARTICLE),
        ("rss_news", {"feed": "https://www.cnbc.com/id/100003114/device/rss/rss.html"}, ARTICLE),
        ("finnhub", {}, ARTICLE),
        ("rss_user", {}, ARTICLE),
        ("ibkr_brfg", {"provider": "BRFG"}, ARTICLE),
        ("ibkr_djnl", {"provider": "DJNL"}, ARTICLE),
        # Mimo tiery: kalendář, sociální obsah bez kurátora
        ("forexfactory", {"impact": "High"}, None),
        ("reddit_rss", {}, None),
        ("bluesky", {"did": "did:plc:x"}, None),
        ("bluesky", {"curated": False}, None),
        ("bluesky", {"curated": "true"}, None),  # jen pravý bool, ne řetězec
        # Neznámý zdroj nemá tier (test pokrytí registru hlídá, aby to nebyla náhoda)
        ("nový_zdroj", {}, None),
    ],
)
def test_content_tier_mapping(source: str, raw: dict[str, object] | None, tier: int | None) -> None:
    assert content_tier(source, raw) == tier


@pytest.mark.parametrize("source", sorted(source for source, *_ in NEWS_SOURCE_SEED))
def test_every_registered_source_has_tier_decision(source: str) -> None:
    """Nový zdroj v registru bez rozhodnutí o tieru by tiše dostal NULL a nikdy
    se nedostal na kartu breaking news — musí být v `FIXED_TIERS` (i jako None),
    mezi zdroji s tierem podle payloadu, nebo IBKR pásek."""
    assert source in FIXED_TIERS or source in PAYLOAD_SOURCES or source.startswith(IBKR_PREFIX)


# ── Efektivní tier a nejdřívější publikace (ADR-0059 bod 3, #1489) ──

T0 = dt.datetime(2026, 10, 9, 12, 30, tzinfo=dt.UTC)


def copy(tier: int | None, *, published_s: int, fetched_s: int) -> SourceCopy:
    return SourceCopy(
        content_tier=tier,
        published_at=T0 + dt.timedelta(seconds=published_s),
        fetched_at=T0 + dt.timedelta(seconds=fetched_s),
    )


def test_effective_tier_takes_lowest_visible_delivery() -> None:
    """CNBC (3) dřív než Newsdesk (2): od příchodu kopie má zpráva tier 2."""
    newsdesk = copy(HEADLINE, published_s=-1, fetched_s=40)
    assert effective_tier(ARTICLE, [newsdesk], at=T0 + dt.timedelta(seconds=39)) == ARTICLE
    assert effective_tier(ARTICLE, [newsdesk], at=T0 + dt.timedelta(seconds=40)) == HEADLINE


def test_effective_tier_ignores_null_tiers() -> None:
    assert effective_tier(None, [], at=T0) is None
    assert effective_tier(None, [copy(None, published_s=0, fetched_s=0)], at=T0) is None
    assert effective_tier(None, [copy(ARTICLE, published_s=0, fetched_s=0)], at=T0) == ARTICLE
    assert effective_tier(HEADLINE, [copy(None, published_s=0, fetched_s=0)], at=T0) == HEADLINE


def test_earliest_published_uses_only_visible_copies() -> None:
    """`ts_event` se nepřepisuje; nejdřívější publikace je odvozená při čtení."""
    earlier = copy(HEADLINE, published_s=-30, fetched_s=60)
    assert earliest_published(T0, [], at=T0) == T0
    assert earliest_published(T0, [earlier], at=T0 + dt.timedelta(seconds=59)) == T0
    assert earliest_published(T0, [earlier], at=T0 + dt.timedelta(seconds=60)) == (
        T0 - dt.timedelta(seconds=30)
    )
    later = copy(ARTICLE, published_s=90, fetched_s=95)
    assert earliest_published(T0, [later], at=T0 + dt.timedelta(minutes=5)) == T0
