"""Karta breaking news — výběr, potvrzení, skupina a téma (ADR-0059 bod 4, #1491).

Golden: změna pravidla nebo slovníku témat = změna očekávání ve stejném PR
s odůvodněním.
"""

import datetime as dt

import pytest

from gexlens_engine.compute.news_tier import SourceCopy, effective_tier
from gexlens_engine.storage.sentiment import NEWS_CATEGORIES
from gexlens_news import breaking, classifier

T0 = dt.datetime(2026, 10, 9, 13, 0, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("tier", "kind", "importance", "category", "expected"),
    [
        (1, "headline", 3, "FED", True),
        (2, "headline", 2, "GEOPOLITICS", True),
        (3, "headline", 2, "MACRO_INFLATION", True),  # revize 9. 10.: článek jde hned
        (None, "social", 3, "FED", False),  # Bluesky bez kurátora, Reddit
        (None, "scheduled", 3, "MACRO_INFLATION", False),  # kalendář má vlastní cestu
        (2, "headline", 1, "FED", False),  # nevýznamná
        (3, "headline", 1, "GEOPOLITICS", False),  # nevýznamný článek
        (2, "headline", None, None, False),  # neklasifikovaná
        (2, "headline", 3, "EARNINGS", False),  # ani mega caps (měření nesplněno)
        (1, "headline", 2, "EARNINGS", False),
        (2, "headline", 2, "TECH", True),
    ],
)
def test_is_breaking(
    tier: int | None, kind: str, importance: int | None, category: str | None, expected: bool
) -> None:
    assert breaking.is_breaking(tier, kind, importance, category) is expected


def test_confirmed_by_visible_copy_tier_1_or_2() -> None:
    copy = SourceCopy(content_tier=2, published_at=T0, fetched_at=T0 + dt.timedelta(minutes=4))
    before = effective_tier(3, [copy], at=T0 + dt.timedelta(minutes=3))
    after = effective_tier(3, [copy], at=T0 + dt.timedelta(minutes=4))
    assert breaking.is_breaking(before, "headline", 2, "FED")
    assert not breaking.is_confirmed(before)  # štítek „článek, zatím nepotvrzeno“
    assert breaking.is_confirmed(after)  # štítek zmizí s viditelnou kopií


def test_copy_tier_3_does_not_confirm_and_null_copy_admits_nothing() -> None:
    article = SourceCopy(content_tier=3, published_at=T0, fetched_at=T0)
    social = SourceCopy(content_tier=None, published_at=T0, fetched_at=T0)
    assert not breaking.is_confirmed(effective_tier(3, [article], at=T0))
    # první doručení mimo tiery + kopie článku → na kartu jako nepotvrzená
    tier = effective_tier(None, [article, social], at=T0)
    assert breaking.is_breaking(tier, "headline", 2, "FED")
    assert not breaking.is_confirmed(tier)
    assert not breaking.is_breaking(effective_tier(None, [social], at=T0), "headline", 3, "FED")


@pytest.mark.parametrize(("tier", "expected"), [(1, True), (2, True), (3, False), (None, False)])
def test_is_confirmed(tier: int | None, expected: bool) -> None:
    assert breaking.is_confirmed(tier) is expected


@pytest.mark.parametrize(
    ("category", "source", "group"),
    [
        ("MACRO_INFLATION", "alpaca", "macro"),
        ("MACRO_LABOR", "alpaca", "macro"),
        ("MACRO_GROWTH", "alpaca", "macro"),
        ("FED", "fed_rss", "central_banks"),
        ("OTHER", "ecb", "central_banks"),  # zdroj ECB bez ohledu na kategorii
        ("GEOPOLITICS", "bluesky", "geopolitics"),
        ("ENERGY", "alpaca", "geopolitics"),
        ("EARNINGS", "alpaca", "companies"),
        ("TECH", "rss_news", "companies"),
        ("CRYPTO", "alpaca", "other"),
        ("OTHER", "alpaca", "other"),
        (None, "alpaca", "other"),
        ("NEZNÁMÁ", "alpaca", "other"),
    ],
)
def test_card_group(category: str | None, source: str, group: str) -> None:
    assert breaking.card_group(category, source) == group


def test_every_category_has_explicit_group() -> None:
    assert set(breaking.CATEGORY_GROUPS) == set(NEWS_CATEGORIES)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Fed holds rates steady, cites tariff uncertainty", "fed"),
        ("Fed's Waller says Iran war raises inflation risk", "fed"),
        ("Trump announces 50% tariffs on Chinese goods", "tariffs"),
        ("US, Japan reach trade deal", "tariffs"),
        ("Iran threatens to close Strait of Hormuz", "iran"),
        ("Israeli strikes hit targets near the border", "israel"),
        ("Tankers turn away from Hormuz", "hormuz"),
        ("Taiwan reports Chinese warplanes near the island", "china"),
        ("OPEC+ agrees to raise output in November", "opec"),
        ("Houthis claim attack on ship in the Red Sea", "middle_east"),
        # předmět před zmínkou: „Oil jumps“ je předmět, Írán důvod za spojkou
        ("Oil jumps as Iran seizes tanker", "energy"),
        # předmět bez tématu → téma z celého titulku
        ("Stocks slide as Iran tensions rise", "iran"),
        ("US CPI rises 0.3% in September, core CPI 0.2%", "inflation"),
        ("Nonfarm payrolls rise by 150K, unemployment rate 4.3%", "labor"),
        ("Philly Fed manufacturing index rises to 12.4", "growth"),
        ("US retail sales beat estimates", "growth"),
        ("Government shutdown looms after Senate vote fails", "fiscal"),
        ("Moody's downgrades US credit rating to Aa1", "fiscal"),
        ("<b>Fed</b> minutes released https://example.com/x", "fed"),
        ("Apple unveils new iPhone lineup", None),
        ("", None),
    ],
)
def test_theme(title: str, expected: str | None) -> None:
    assert breaking.theme(title) == expected


def test_every_regime_actor_is_a_theme() -> None:
    keys = {key for key, _ in breaking.THEMES}
    assert set(classifier.REGIME_ACTORS) <= keys


@pytest.mark.parametrize(
    "title",
    [
        "Iranian drones hit tanker",
        "Tehran rejects talks",
        "Saudi Arabia raises prices",
        "Riyadh summit ends",
        "Yemeni rebels fire missile",
        "Middle East tensions",
        "Gulf states meet",
        "Beijing responds",
        "Israel strikes",
        "Ukraine talks continue",  # mimo režim
        "Iranians protest",  # „iranians“ není aktér (hranice slova)
    ],
)
def test_actors_union_matches_like_named_actors(title: str) -> None:
    named = any(
        pattern.search(title) for key, pattern in breaking.THEMES if key in classifier.REGIME_ACTORS
    )
    assert bool(classifier.ACTORS.search(title)) is named
