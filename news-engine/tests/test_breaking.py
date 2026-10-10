"""Karta breaking news — výběr, potvrzení, skupina a téma (ADR-0059 bod 4, #1491).

Golden: změna pravidla nebo slovníku témat = změna očekávání ve stejném PR
s odůvodněním.
"""

import datetime as dt

import pytest

from gexlens_engine.compute.news_tier import SourceCopy, effective_tier
from gexlens_engine.storage.sentiment import NEWS_CATEGORIES
from gexlens_news import breaking, classifier
from gexlens_news.reactions import Bar, compute_reactions, measure_excursion

T0 = dt.datetime(2026, 10, 9, 13, 0, tzinfo=dt.UTC)
#: Zpoždění příjmu, které pravidlo čerstvosti (varianta C) nevyřadí
FRESH = dt.timedelta(minutes=1)


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
    assert breaking.is_breaking(tier, kind, importance, category, ingest_delay=FRESH) is expected


def test_confirmed_by_visible_copy_tier_1_or_2() -> None:
    copy = SourceCopy(content_tier=2, published_at=T0, fetched_at=T0 + dt.timedelta(minutes=4))
    before = effective_tier(3, [copy], at=T0 + dt.timedelta(minutes=3))
    after = effective_tier(3, [copy], at=T0 + dt.timedelta(minutes=4))
    assert breaking.is_breaking(before, "headline", 2, "FED", ingest_delay=FRESH)
    assert not breaking.is_confirmed(before)  # štítek „článek, zatím nepotvrzeno“
    assert breaking.is_confirmed(after)  # štítek zmizí s viditelnou kopií


def test_copy_tier_3_does_not_confirm_and_null_copy_admits_nothing() -> None:
    article = SourceCopy(content_tier=3, published_at=T0, fetched_at=T0)
    social = SourceCopy(content_tier=None, published_at=T0, fetched_at=T0)
    assert not breaking.is_confirmed(effective_tier(3, [article], at=T0))
    # první doručení mimo tiery + kopie článku → na kartu jako nepotvrzená
    tier = effective_tier(None, [article, social], at=T0)
    assert breaking.is_breaking(tier, "headline", 2, "FED", ingest_delay=FRESH)
    assert not breaking.is_confirmed(tier)
    assert not breaking.is_breaking(
        effective_tier(None, [social], at=T0), "headline", 3, "FED", ingest_delay=FRESH
    )


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


@pytest.mark.parametrize(
    ("tier", "delay_min", "expected"),
    [
        (3, 60, True),  # varianta C: článek nejvýš 60 min po publikaci
        (3, 61, False),  # starý článek (Finnhub doručuje Reuters s mediánem 11 h)
        (2, 600, True),  # headline feed omezení nemá
        (1, 600, True),
    ],
)
def test_article_must_be_fresh(tier: int, delay_min: int, expected: bool) -> None:
    delay = dt.timedelta(minutes=delay_min)
    assert breaking.is_breaking(tier, "headline", 2, "FED", ingest_delay=delay) is expected


# ── Dopad na kartě (E-6.28b, ADR-0059 bod 8) ─────────────────────────

EVENT = dt.datetime(2026, 10, 8, 14, 30, 20, tzinfo=dt.UTC)
MINUTE_START = EVENT.replace(second=0)


def minute_bars(first: dt.datetime, last: dt.datetime) -> list[Bar]:
    """Klidná hodina před zprávou (σ pro výchylku), od minuty zprávy růst o 2 body/min."""
    bars: list[Bar] = []
    ts = first
    while ts <= last:
        offset = int((ts - MINUTE_START) / dt.timedelta(minutes=1))
        base = 5000.0 + (0.25 if offset % 2 else -0.25)
        price = base if offset < 0 else 5000.0 + 2.0 * (offset + 1)
        bars.append(
            Bar(ts=ts, open=price, high=price + 0.5, low=price - 0.5, close=price, volume=10)
        )
        ts += dt.timedelta(minutes=1)
    return bars


BEFORE = MINUTE_START - dt.timedelta(minutes=70)


def test_card_impact_runs_from_available_bars() -> None:
    at = EVENT + dt.timedelta(minutes=2, seconds=10)  # 14:32:30, rozpracovaná minuta 14:32
    bars = minute_bars(BEFORE, MINUTE_START + dt.timedelta(minutes=2))
    impact = breaking.card_impact(EVENT, bars, at=at, market_closed=False)
    (expected,) = compute_reactions(EVENT, bars, windows=(5,))
    assert impact.state == breaking.IMPACT_RUNNING
    assert impact.elapsed_min == 2
    assert impact.ret_bp == pytest.approx(expected.ret_bp)
    assert impact.ret_bp is not None and impact.ret_bp > 0
    excursion = measure_excursion(bars, MINUTE_START, 2)  # okno roste s uzavřenými minutami
    assert excursion is not None
    assert impact.excursion_bp == pytest.approx(excursion.bp)
    assert impact.excursion_direction == 1
    assert not impact.contaminated


def test_card_impact_is_fixed_after_five_minutes_like_news_reactions() -> None:
    bars = minute_bars(BEFORE, MINUTE_START + dt.timedelta(minutes=30))
    at = EVENT + dt.timedelta(minutes=25)
    impact = breaking.card_impact(EVENT, bars, at=at, market_closed=False)
    stored = {r.window_min: r for r in compute_reactions(EVENT, bars)}  # cesta news_reactions
    assert impact.state == breaking.IMPACT_FIXED
    assert impact.ret_bp == pytest.approx(stored[5].ret_bp)
    assert impact.range_bp == pytest.approx(stored[5].range_bp)
    excursion = measure_excursion(bars, MINUTE_START, 5)
    assert excursion is not None
    assert impact.excursion_bp == pytest.approx(excursion.bp)
    # Hodnota se po 5. minutě nemění
    later = breaking.card_impact(EVENT, bars, at=at + dt.timedelta(hours=1), market_closed=False)
    assert later.ret_bp == impact.ret_bp and later.excursion_bp == impact.excursion_bp


def test_card_impact_market_closed_and_gaps() -> None:
    bars = minute_bars(BEFORE, MINUTE_START + dt.timedelta(minutes=10))
    closed = breaking.card_impact(
        EVENT, bars, at=EVENT + dt.timedelta(minutes=3), market_closed=True
    )
    assert closed.state == breaking.IMPACT_CLOSED and closed.ret_bp is None

    only_before = minute_bars(BEFORE, MINUTE_START - dt.timedelta(minutes=1))
    waiting = breaking.card_impact(
        EVENT, only_before, at=EVENT + dt.timedelta(seconds=30), market_closed=False
    )
    assert waiting.state == breaking.IMPACT_RUNNING and waiting.ret_bp is None
    gap = breaking.card_impact(
        EVENT, only_before, at=EVENT + dt.timedelta(minutes=7), market_closed=False
    )
    assert gap.state == breaking.IMPACT_NO_DATA and gap.ret_bp is None


def test_card_impact_contamination_k1() -> None:
    bars = minute_bars(BEFORE, MINUTE_START + dt.timedelta(minutes=10))
    other = EVENT + dt.timedelta(minutes=2)
    impact = breaking.card_impact(
        EVENT, bars, at=EVENT + dt.timedelta(minutes=8), other_event_ts=[other], market_closed=False
    )
    assert impact.contaminated


@pytest.mark.parametrize(
    ("ts_event", "expected"),
    [
        (EVENT, dt.datetime(2026, 10, 8, 14, 36, tzinfo=dt.UTC)),  # bar 14:35 se uzavře v 14:36
        (MINUTE_START, dt.datetime(2026, 10, 8, 14, 35, tzinfo=dt.UTC)),  # poslední bar 14:34
    ],
)
def test_impact_fixes_when_last_window_bar_closes(
    ts_event: dt.datetime, expected: dt.datetime
) -> None:
    assert breaking.fixed_at(ts_event) == expected


def test_card_impact_runs_until_last_window_bar_closes() -> None:
    bars = minute_bars(BEFORE, MINUTE_START + dt.timedelta(minutes=5))
    at = EVENT + dt.timedelta(minutes=5, seconds=10)  # 14:35:30, bar 14:35 ještě běží
    impact = breaking.card_impact(EVENT, bars, at=at, market_closed=False)
    assert impact.state == breaking.IMPACT_RUNNING
    later = breaking.card_impact(
        EVENT, bars, at=dt.datetime(2026, 10, 8, 14, 36, tzinfo=dt.UTC), market_closed=False
    )
    assert later.state == breaking.IMPACT_FIXED


def test_news_just_before_closure_is_closed_not_missing_data() -> None:
    """Zpráva v poslední minutě před pauzou nebo víkendem: bez barů po zprávě
    nebo s odloženou reakcí je to zavřený trh, ne mezera (žádné „bez dat“ přes
    víkend)."""
    ts = dt.datetime(2026, 10, 9, 20, 59, 30, tzinfo=dt.UTC)  # pátek 15:59:30 CT
    friday = [
        Bar(ts.replace(second=0) - dt.timedelta(minutes=n), 5000.0, 5000.5, 4999.5, 5000.0, 1.0)
        for n in range(70, -1, -1)
    ]
    saturday = ts + dt.timedelta(hours=10)
    impact = breaking.card_impact(ts, friday, at=saturday, market_closed=False, window_closed=True)
    assert impact.state == breaking.IMPACT_CLOSED
    sunday_open = dt.datetime(2026, 10, 11, 22, 0, tzinfo=dt.UTC)
    resumed = [*friday, Bar(sunday_open, 5010.0, 5010.5, 5009.5, 5010.0, 1.0)]
    deferred = breaking.card_impact(
        ts, resumed, at=sunday_open + dt.timedelta(minutes=10), market_closed=False,
        window_closed=True,
    )  # fmt: skip
    assert deferred.state == breaking.IMPACT_CLOSED
    # Tatáž mezera při otevřeném trhu je výpadek dat
    gap = breaking.card_impact(ts, friday, at=saturday, market_closed=False)
    assert gap.state == breaking.IMPACT_NO_DATA
