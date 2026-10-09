"""Tier obsahu zprávy — mapování ADR-0059 bod 1 a pokrytí registru zdrojů (#1486)."""

import pytest

from gexlens_engine.compute.news_tier import (
    ARTICLE,
    FIXED_TIERS,
    HEADLINE,
    IBKR_PREFIX,
    OFFICIAL,
    PAYLOAD_SOURCES,
    content_tier,
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
