"""Testy SentIndexu a topic indexů (#283): decay, kontinuita, aktivace, OHLC."""

import datetime as dt

import pytest

from gexlens_news.sentindex import (
    TOPIC_MIN_EVENTS,
    ScoredEvent,
    daily_ohlc,
    decayed_contribution,
    half_life_minutes,
    sent_index,
    sent_index_series,
    topic_indexes,
)

NOON = dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.UTC)


def event(
    score: float,
    *,
    minutes_ago: float = 0.0,
    category: str = "FED",
    importance: int = 3,
) -> ScoredEvent:
    return ScoredEvent(
        ts_event=NOON - dt.timedelta(minutes=minutes_ago),
        category=category,
        importance=importance,
        score=score,
    )


# ── Poločas a dohasínání ───────────────────────────────────────────


def test_half_life_scales_with_category_and_importance() -> None:
    # FED drží déle než TECH
    assert half_life_minutes("FED", 2) > half_life_minutes("TECH", 2)
    # Vyšší důležitost prodlužuje dozvuk
    assert half_life_minutes("FED", 3) > half_life_minutes("FED", 1)
    # Neznámá kategorie spadne na fallback, ne na nulu
    assert half_life_minutes("NEZNAMA", 2) > 0


def test_contribution_halves_after_one_half_life() -> None:
    tau = half_life_minutes("FED", 3)
    fresh = decayed_contribution(event(1.0), NOON)
    aged = decayed_contribution(event(1.0, minutes_ago=tau), NOON)
    assert fresh == pytest.approx(1.0)
    assert aged == pytest.approx(0.5, rel=1e-6)
    # Po dvou poločasech čtvrtina
    older = decayed_contribution(event(1.0, minutes_ago=2 * tau), NOON)
    assert older == pytest.approx(0.25, rel=1e-6)


def test_future_event_contributes_nothing() -> None:
    """Plánovaný event, který ještě nenastal, index neovlivňuje."""
    upcoming = ScoredEvent(NOON + dt.timedelta(hours=2), "MACRO_INFLATION", 3, 1.0)
    assert decayed_contribution(upcoming, NOON) == 0.0
    assert sent_index([upcoming], NOON) == 0.0


# ── Kontinuita (klíčové rozhodnutí SPEC v1.3) ──────────────────────


def test_index_is_continuous_across_midnight() -> None:
    """Žádný reset seance — noční zpráva doznívá do rána.

    Kdyby se index resetoval, ranní hodnota by byla nula a denní svíčka by
    měla vždy nulový open (SPEC 7.1 by ztratila smysl).
    """
    evening = dt.datetime(2026, 7, 27, 22, 0, tzinfo=dt.UTC)
    news = [ScoredEvent(evening, "GEOPOLITICS", 3, -1.0)]

    at_night = sent_index(news, evening + dt.timedelta(hours=1))
    next_morning = sent_index(news, evening + dt.timedelta(hours=10))
    assert at_night < 0
    assert next_morning < 0  # pořád záporný, jen slabší
    assert abs(next_morning) < abs(at_night)


def test_opposite_events_cancel_out() -> None:
    mixed = [event(1.0, category="FED"), event(-1.0, category="FED")]
    assert sent_index(mixed, NOON) == pytest.approx(0.0)


def test_negligible_contributions_are_dropped() -> None:
    """Dávno vyhaslá zpráva index nezatěžuje."""
    ancient = event(1.0, minutes_ago=60 * 24 * 30)  # měsíc stará
    assert sent_index([ancient], NOON) == 0.0


# ── Řada a OHLC ────────────────────────────────────────────────────


def test_series_has_one_point_per_minute() -> None:
    series = sent_index_series([event(1.0)], NOON, NOON + dt.timedelta(minutes=9))
    assert len(series) == 10
    assert series[0][0] == NOON
    # Bez nové zprávy index monotónně klesá k nule
    assert series[0][1] > series[-1][1] > 0


def test_series_handles_reversed_range() -> None:
    assert sent_index_series([event(1.0)], NOON, NOON - dt.timedelta(hours=1)) == []


def test_daily_ohlc_open_reflects_overnight_carryover() -> None:
    """Open ≠ 0 je celý smysl kontinuálního indexu."""
    evening = dt.datetime(2026, 7, 27, 22, 0, tzinfo=dt.UTC)
    news = [ScoredEvent(evening, "GEOPOLITICS", 3, -2.0)]
    start = dt.datetime(2026, 7, 28, 0, 0, tzinfo=dt.UTC)
    series = sent_index_series(news, start, start + dt.timedelta(hours=6), step_minutes=10)

    candle = daily_ohlc(series, dt.date(2026, 7, 28))
    assert candle is not None
    assert candle.open < 0  # z noci něco zbylo
    assert candle.low <= candle.close <= candle.high
    # Bez nové zprávy index doznívá k nule → close je blíž nule než open
    assert abs(candle.close) < abs(candle.open)


def test_daily_ohlc_without_data_is_none() -> None:
    series = sent_index_series([event(1.0)], NOON, NOON + dt.timedelta(minutes=5))
    assert daily_ohlc(series, dt.date(2020, 1, 1)) is None


# ── Topic indexy ───────────────────────────────────────────────────


def test_topic_activates_only_with_enough_recent_events() -> None:
    """Jedna zpráva není narativ, který hýbe trhem (SPEC 5.5)."""
    single = topic_indexes([event(1.0, category="ENERGY")], NOON)
    assert not single[0].active
    assert single[0].events_in_window == 1

    many = [event(1.0, category="ENERGY", minutes_ago=i * 10) for i in range(TOPIC_MIN_EVENTS)]
    assert topic_indexes(many, NOON)[0].active


def test_old_events_count_to_value_but_not_to_activation() -> None:
    """Aktivace se řídí čerstvostí, hodnota dohasínáním — jsou to dvě věci."""
    old = [
        event(1.0, category="FED", minutes_ago=60 * 30) for _ in range(TOPIC_MIN_EVENTS)
    ]  # 30 h zpět, mimo 24h okno
    fresh = [event(1.0, category="FED", minutes_ago=5)]
    topics = topic_indexes(old + fresh, NOON)
    assert topics[0].events_in_window == 1
    assert not topics[0].active


def test_topics_sorted_by_absolute_value() -> None:
    events = [
        event(0.2, category="TECH"),
        event(-3.0, category="GEOPOLITICS"),
        event(1.0, category="FED"),
    ]
    topics = topic_indexes(events, NOON)
    assert [t.category for t in topics] == ["GEOPOLITICS", "FED", "TECH"]
    # Záporný narativ se řadí podle síly, ne podle znaménka
    assert topics[0].value < 0


def test_topic_value_matches_filtered_index() -> None:
    events = [event(1.0, category="FED"), event(-1.0, category="TECH")]
    topics = {t.category: t.value for t in topic_indexes(events, NOON)}
    assert topics["FED"] == pytest.approx(sent_index([events[0]], NOON))
    assert topics["TECH"] == pytest.approx(sent_index([events[1]], NOON))


def test_topic_series_splits_by_category() -> None:
    """#566 fáze 1: řada per téma je index filtrovaný na kategorii."""
    from gexlens_news.sentindex import topic_series

    events = [
        event(1.0, minutes_ago=30, category="FED"),
        event(-0.5, minutes_ago=30, category="ENERGY"),
    ]
    series = topic_series(events, NOON - dt.timedelta(minutes=10), NOON, step_minutes=10)
    assert set(series) == {"FED", "ENERGY"}
    assert len(series["FED"]) == 2  # start a end při kroku 10 min
    # Hodnota tématu = sent_index jen z jeho eventů
    assert series["FED"][-1][1] == pytest.approx(sent_index([events[0]], NOON))
    assert series["ENERGY"][-1][1] < 0


def test_topic_shares_weight_and_order() -> None:
    """#566 fáze 2: |score|·importance faktor, směr se ruší nesmí — a řadí se sestupně."""
    from gexlens_news.sentindex import topic_shares

    start, end = NOON - dt.timedelta(hours=2), NOON
    events = [
        # FED: dvě zprávy proti sobě — téma se „řeší", i když se směrově ruší
        event(1.0, minutes_ago=30, category="FED", importance=3),
        event(-1.0, minutes_ago=40, category="FED", importance=3),
        event(0.5, minutes_ago=20, category="TECH", importance=1),
        # Mimo období — nepočítá se
        event(9.0, minutes_ago=600, category="ENERGY"),
    ]
    shares = topic_shares(events, start, end)
    assert [s.category for s in shares] == ["FED", "TECH"]
    fed, tech = shares
    assert fed.events == 2
    assert fed.weight == pytest.approx(2 * 1.0 * 1.5)  # |±1| · faktor důležitosti 3
    assert tech.weight == pytest.approx(0.5 * 0.5)
    assert fed.share + tech.share == pytest.approx(1.0)
    assert fed.share > tech.share


def test_topic_shares_empty_period() -> None:
    from gexlens_news.sentindex import topic_shares

    assert topic_shares([], NOON - dt.timedelta(hours=1), NOON) == []
