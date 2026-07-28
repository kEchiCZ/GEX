"""SentIndex a topic indexy (#283, SPEC 5.4 a 5.5) — čisté funkce bez I/O.

Běžící suma vážených impact skóre s exponenciálním dohasínáním:

    SentIndex(t) = Σ_e score(e) · exp(−(t − ts_e) / τ_e)

**Index je kontinuální — žádný reset na začátku seance** (rozhodnutí SPEC v1.3).
Decay běží 24/7, takže starý sentiment vyhasne sám, ale overnight a víkendové
zprávy korektně doznívají do open. Ranní hodnota tedy říká, co z noci reálně
zbylo — a díky tomu má smysl i denní OHLC svíčka (7.1), která by při resetu
měla open vždy nulový.

Topic index je tentýž výpočet filtrovaný na kategorii. Aktivuje se, až má
kategorie dost čerstvých eventů — jinak by jediná zpráva vypadala jako
„narativ, který hýbe trhem".
"""

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# Poločas rozpadu per kategorie (minuty). Vědomé odhady, ne měření — SPEC kap. 4
# říká, že half-life je empirický parametr; než se nasbírá dost reakcí, jsou to
# defaulty odvozené z toho, jak dlouho který typ zprávy obvykle rezonuje.
DEFAULT_HALF_LIFE_MIN: Mapping[str, float] = {
    "FED": 180.0,
    "GEOPOLITICS": 240.0,
    "MACRO_INFLATION": 120.0,
    "MACRO_LABOR": 120.0,
    "MACRO_GROWTH": 120.0,
    "ENERGY": 90.0,
    "EARNINGS": 60.0,
    "TECH": 45.0,
    "CRYPTO": 45.0,
    "OTHER": 30.0,
}
FALLBACK_HALF_LIFE_MIN = 30.0
# Důležitost prodlužuje dozvuk: okrajová zpráva vyhasne dřív než klíčová
IMPORTANCE_FACTOR: Mapping[int, float] = {1: 0.5, 2: 1.0, 3: 1.5}

# Aktivace topic indexu (SPEC 5.5): kolik eventů kategorie za jaké okno
TOPIC_MIN_EVENTS = 5
TOPIC_WINDOW_HOURS = 24
# Příspěvek pod tímhle prahem se zahazuje — dávno vyhaslé zprávy jinak
# nekonečně dlouho zabírají paměť i výpočet
NEGLIGIBLE_CONTRIBUTION = 1e-4


@dataclass(frozen=True)
class ScoredEvent:
    """Událost připravená ke vstupu do indexu."""

    ts_event: dt.datetime
    category: str
    importance: int
    # direction × strength × w_cat (váhy z kalibrace zatím 1.0, SPEC 5.3)
    score: float


@dataclass(frozen=True)
class TopicIndex:
    category: str
    value: float
    events_in_window: int

    @property
    def active(self) -> bool:
        """Kategorie hýbe narativem, až když má dost čerstvých zpráv."""
        return self.events_in_window >= TOPIC_MIN_EVENTS


def half_life_minutes(
    category: str,
    importance: int,
    overrides: Mapping[str, float] | None = None,
) -> float:
    """τ pro kategorii a důležitost; `overrides` umožní kalibrované hodnoty."""
    table = overrides or DEFAULT_HALF_LIFE_MIN
    base = table.get(category, FALLBACK_HALF_LIFE_MIN)
    return base * IMPORTANCE_FACTOR.get(importance, 1.0)


def decayed_contribution(
    event: ScoredEvent, at: dt.datetime, *, overrides: Mapping[str, float] | None = None
) -> float:
    """Příspěvek jedné události k indexu v čase `at`.

    Budoucí událost (plánovaný event, který ještě nenastal) přispívá nulou —
    index popisuje, co už trh ví, ne co teprve přijde.
    """
    age_min = (at - event.ts_event).total_seconds() / 60.0
    if age_min < 0:
        return 0.0
    tau = half_life_minutes(event.category, event.importance, overrides)
    if tau <= 0:
        return 0.0
    # exp(−t/τ) s τ jako poločasem → po τ minutách zbývá polovina
    return event.score * math.exp(-age_min * math.log(2) / tau)


def sent_index(
    events: Sequence[ScoredEvent],
    at: dt.datetime,
    *,
    overrides: Mapping[str, float] | None = None,
) -> float:
    """Hodnota indexu v jednom okamžiku."""
    total = 0.0
    for event in events:
        contribution = decayed_contribution(event, at, overrides=overrides)
        if abs(contribution) >= NEGLIGIBLE_CONTRIBUTION:
            total += contribution
    return total


def sent_index_series(
    events: Sequence[ScoredEvent],
    start: dt.datetime,
    end: dt.datetime,
    *,
    step_minutes: int = 1,
    overrides: Mapping[str, float] | None = None,
) -> list[tuple[dt.datetime, float]]:
    """1min řada indexu v intervalu (SPEC 5.4) — podklad pro panel i OHLC."""
    if end < start:
        return []
    series: list[tuple[dt.datetime, float]] = []
    moment = start
    step = dt.timedelta(minutes=step_minutes)
    while moment <= end:
        series.append((moment, sent_index(events, moment, overrides=overrides)))
        moment += step
    return series


def topic_indexes(
    events: Sequence[ScoredEvent],
    at: dt.datetime,
    *,
    overrides: Mapping[str, float] | None = None,
) -> list[TopicIndex]:
    """Index per kategorie, seřazený dle |hodnoty| (SPEC 5.5).

    Vrací i neaktivní kategorie — o zobrazení rozhoduje volající podle
    `active`, aby šlo ukázat i „skoro aktivní" narativ.
    """
    window_start = at - dt.timedelta(hours=TOPIC_WINDOW_HOURS)
    by_category: dict[str, list[ScoredEvent]] = {}
    for event in events:
        by_category.setdefault(event.category, []).append(event)

    result = [
        TopicIndex(
            category=category,
            value=sent_index(items, at, overrides=overrides),
            events_in_window=sum(1 for e in items if window_start <= e.ts_event <= at),
        )
        for category, items in by_category.items()
    ]
    return sorted(result, key=lambda topic: abs(topic.value), reverse=True)


@dataclass(frozen=True)
class DailyOhlc:
    date: dt.date
    open: float
    high: float
    low: float
    close: float


def daily_ohlc(series: Sequence[tuple[dt.datetime, float]], day: dt.date) -> DailyOhlc | None:
    """OHLC denní svíčky sentimentu (SPEC 7.1); None = pro daný den nejsou data.

    Díky absenci session resetu je `open` smysluplný — ukazuje, co z overnight
    a víkendových zpráv do rána zbylo.
    """
    values = [value for moment, value in series if moment.date() == day]
    if not values:
        return None
    return DailyOhlc(
        date=day,
        open=values[0],
        high=max(values),
        low=min(values),
        close=values[-1],
    )
