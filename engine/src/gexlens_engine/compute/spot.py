"""Spot minutového cyklu se stářím (#1600, #1623; ADR-0057 doplněk 23. 10.).

Minutový cyklus dřív bral tasty override, jinak `ticker.last`, jinak
`marketPrice()`, jinak poslední uloženou hodnotu — vždy číslo, nikdy čas.
Zamrzlá cena tak hlasovala ve verdiktu dne, v tendenci i v setupech jako živá:
ES 7. 10. 18:51–18:55 UTC stál spot na 7854,62 (mid), zatímco bar 18:55 měl
rozsah 7855,5–7857,0. Během fallbacku na tasty (#614) totiž `fallback_price`
drží poslední cenu i poté, co tasty přestane být čerstvá.

Pořadí zdrojů (Rozhodnuto 23. 10. v #1385, rozsah A):

1. **IBKR ticker** — `ticker.last` při živém feedu (tick nejvýš `IBKR_MAX_AGE`,
   stejně jako výchozí práh přepnutí na tasty `tasty_spot_stale_after_s`, #614).
   Mid (`marketPrice()`) se nebere — je to symptom #1623, ne cena obchodu;
2. **close posledního 5s baru IBKR** — ticker mlčí, ale bary jdou
   (konec baru nejvýš `BAR_MAX_AGE`);
3. **tasty** — mid front futures, který zdroj hlásí jako čerstvý (kotace nejvýš
   `tasty_spot_max_age_s`, výchozí 30 s); čas je okamžik pollu fallback smyčky,
   takže kotace může být stará až `tasty_spot_max_age_s` + `TASTY_MAX_AGE`.
   Přispívá jen se zapnutým `GEXLENS_TASTY_SPOT_FALLBACK`;
4. jinak spot **chybí**: `SpotReading.live` je None a spotřebitelé ho berou jako
   chybějící vstup (viditelná mezera, ne zmrzlé číslo — SPEC 3.7).

`SpotReading.price` nese i tak nejčerstvější známou cenu: GEX výpočet a obálka
striků s ní běží dál (pod v6 tedy s IBKR nebo close baru místo tasty override),
hlasovat z ní ale nikdo nesmí. Platí pod
mechanikou v6 (`SPOT_AGE_GATE`); v5 počítá dál `instruments._current_spot`.
"""

import datetime as dt
from typing import Literal, NamedTuple

from gexlens_engine.compute.setups import CUMDELTA_PRINTS

SpotSource = Literal["ibkr", "bar", "tasty"]

#: Spot se stářím zapíná bump v6 (#1403) spolu s CumΔ z tisků (ADR-0057 bod 11)
SPOT_AGE_GATE = CUMDELTA_PRINTS
#: Feed IBKR je živý, když poslední tick s `last` není starší (výchozí práh fallbacku #614)
IBKR_MAX_AGE = dt.timedelta(seconds=30)
#: Close 5s baru platí, když bar skončil nejvýš před minutou (bary chodí à 5 s)
BAR_MAX_AGE = dt.timedelta(seconds=60)
#: Poslední poll, ve kterém tasty hlásila čerstvou kotaci, nejvýš 30 s zpět
TASTY_MAX_AGE = dt.timedelta(seconds=30)
#: Po otevření trhu (a po startu) se chybějící spot chvíli nehlásí: první ticky
#: přicházejí až po otevření a 17:00:00 by jinak varovalo každý den
SPOT_WATCH_GRACE = dt.timedelta(minutes=2)


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
        return f"spot: poslední cena {self.price:.2f} ({self.source}) stará {age} s"


def bar_close_at(started: dt.datetime, close: float, seconds: int = 5) -> PriceAt:
    """Close 5s baru IBKR jako cena ke konci baru (`RealTimeBar.time` je začátek)."""
    aware = started if started.tzinfo is not None else started.replace(tzinfo=dt.UTC)
    return PriceAt(close, aware + dt.timedelta(seconds=seconds))


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
