"""Spot minutového cyklu se stářím (#1600, #1623; ADR-0057 doplněk 23. 10.).

Minutový cyklus dřív bral tasty override, jinak `ticker.last`, jinak
`marketPrice()`, jinak poslední uloženou hodnotu — vždy číslo, nikdy čas.
Zamrzlá cena tak hlasovala ve verdiktu dne, v tendenci i v setupech jako živá:
ES 7. 10. 18:51–18:55 UTC stál spot na 7854,62 (mid), zatímco bar 18:55 měl
rozsah 7855,5–7857,0. Během fallbacku na tasty (#614) totiž `fallback_price`
drží poslední cenu i poté, co tasty přestane být čerstvá.

Pořadí zdrojů (Rozhodnuto 23. 10. v #1385, rozsah A):

1. **IBKR ticker** — živý feed (aktualizace nejvýš `IBKR_MAX_AGE`, shodně
   s prahem přepnutí na tasty `tasty.spot_fallback.DEFAULT_STALE_AFTER_S`);
2. **close posledního 5s baru IBKR** — ticker mlčí, ale bary jdou
   (konec baru nejvýš `BAR_MAX_AGE`);
3. **tasty** — čerstvá kotace (nejvýš `TASTY_MAX_AGE`);
4. jinak spot **chybí**: `SpotReading.live` je None a spotřebitelé ho berou jako
   chybějící vstup (viditelná mezera, ne zmrzlé číslo — SPEC 3.7).

`SpotReading.price` nese i tak nejčerstvější známou cenu: GEX výpočet a obálka
striků s ní běží dál jako dosud, hlasovat z ní ale nikdo nesmí. Platí pod
mechanikou v6 (`SPOT_AGE_GATE`); v5 počítá dál `instruments._current_spot`.
"""

import datetime as dt
from typing import Literal, NamedTuple

from gexlens_engine.compute.setups import CUMDELTA_PRINTS

SpotSource = Literal["ibkr", "bar", "tasty"]

#: Spot se stářím zapíná bump v6 (#1403) spolu s CumΔ z tisků (ADR-0057 bod 11)
SPOT_AGE_GATE = CUMDELTA_PRINTS
#: Feed IBKR je živý, když poslední aktualizace tickeru není starší (práh fallbacku #614)
IBKR_MAX_AGE = dt.timedelta(seconds=30)
#: Close 5s baru platí, když bar skončil nejvýš před minutou (bary chodí à 5 s)
BAR_MAX_AGE = dt.timedelta(seconds=60)
#: Kotace tasty platí jako u IBKR nejvýš 30 s
TASTY_MAX_AGE = dt.timedelta(seconds=30)


class PriceAt(NamedTuple):
    """Cena zdroje a okamžik, ke kterému platí (UTC; u 5s baru jeho konec)."""

    price: float
    ts: dt.datetime


class SpotReading(NamedTuple):
    """Spot cyklu: nejčerstvější známá cena, její čas a zdroj, a zda je živá."""

    price: float | None
    ts: dt.datetime | None
    source: SpotSource | None
    fresh: bool

    @property
    def live(self) -> float | None:
        """Cena, ze které se smí hlasovat; None = spot chybí."""
        return self.price if self.fresh else None

    def describe(self, now: dt.datetime) -> str:
        """Důvod chybějícího spotu pro log a `missing` verdiktu."""
        if self.price is None or self.ts is None:
            return "spot: žádná cena"
        age = int((now - self.ts).total_seconds())
        return f"spot: poslední cena {self.price:g} ({self.source}) stará {age} s"


def _usable(candidate: PriceAt | None) -> bool:
    return candidate is not None and candidate.price == candidate.price and candidate.price > 0


def resolve_spot(
    now: dt.datetime,
    *,
    ibkr: PriceAt | None,
    bar: PriceAt | None,
    tasty: PriceAt | None,
) -> SpotReading:
    """Spot k okamžiku `now` (UTC) podle pořadí IBKR → 5s bar → tasty."""
    sources: tuple[tuple[SpotSource, PriceAt | None, dt.timedelta], ...] = (
        ("ibkr", ibkr, IBKR_MAX_AGE),
        ("bar", bar, BAR_MAX_AGE),
        ("tasty", tasty, TASTY_MAX_AGE),
    )
    for source, candidate, max_age in sources:
        if candidate is not None and _usable(candidate) and now - candidate.ts <= max_age:
            return SpotReading(candidate.price, candidate.ts, source, True)
    # Nic čerstvého: nejnovější známá cena jen pro GEX výpočet a důvod v logu
    known = [(source, c) for source, c, _ in sources if c is not None and _usable(c)]
    if not known:
        return SpotReading(None, None, None, False)
    source, latest = max(known, key=lambda item: item[1].ts)
    return SpotReading(latest.price, latest.ts, source, False)
