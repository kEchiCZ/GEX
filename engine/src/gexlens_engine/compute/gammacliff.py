"""Gamma útes po expiraci (#576, fáze 1: jen měření) — čisté funkce bez I/O.

Hypotéza referenčního výkladu: gamma expirace zmizí ze dne na den a den po
velkém odpadu má trh systematicky větší rozsah. U ES/NQ expiruje 0DTE každý
den, takže velikost útesu kolísá (běžně ~15 %, před OPEX ~60 %) a dá se měřit.

Fáze 1 nic nezapíná: denní záznam `gamma_cliff` per (seance, symbol) + dopočet
metrik následující seance. Fáze 2 (≥ 30 dnů/instrument) odpoví čísly, jestli
z toho bude režimová brána, šablona, nebo doložené „efekt není".

Poctivé omezení backfillu: Σ|NetGEX| potřebuje gammu (tedy IV ze snapshotů /
levels řady), kterou drží jen 90denní retence (ADR-0022) — věčný OI archiv
sám o sobě nese jen OI. Zpětný výpočet proto sahá tak daleko, jak sahají
levels partice; ~90 dní na fázi 2 bohatě stačí.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from gexlens_engine.compute.bandregime import (
    BAND_CLASS_NO_ZONE,
    BAND_CLASS_OUTSIDE,
    band_class,
)


@dataclass(frozen=True)
class ExpiryAtSettle:
    """Stav jedné sledované expirace v poslední minutě ≤ settle seance."""

    expiry: str  # YYYYMMDD
    total_gex: float  # NetGEX (se znaménkem)
    flip: float | None
    call_wall: float | None
    put_wall: float | None
    #: Hrubá gamma Σ|NetGEX| přes cenovou mřížku profilu (#576 fáze 1 fix):
    #: NetGEX řetězu umí u 0DTE těsně před settle vynulovat call/put strany
    #: (ES OPEX 21. 8. 2026: net −3 843 → +62 během minut, cliff_share 0,028),
    #: velikost gammy, která odpadne, ale nezmizí. None = profil partice chybí.
    gross_gex: float | None = None


@dataclass(frozen=True)
class CliffRecord:
    """Denní záznam odpadu gammy — řádek tabulky `gamma_cliff`."""

    session_date: dt.date
    symbol: str
    gex_before: float
    gex_expiring: float
    cliff_share: float | None  # hlavní veličina; None když gex_before == 0
    is_opex: bool
    # Posun struktury po odpadu: zbytkový profil (nejbližší přeživší expirace)
    # minus settlující řetěz. Kladný = struktura se přesune výš.
    flip_shift: float | None
    call_wall_shift: float | None
    put_wall_shift: float | None


def is_opex_day(day: dt.date) -> bool:
    """Třetí pátek v měsíci (měsíční OPEX)."""
    if day.weekday() != 4:
        return False
    return 15 <= day.day <= 21


def _shift(new: float | None, old: float | None) -> float | None:
    if new is None or old is None:
        return None
    return new - old


def gex_magnitude(item: ExpiryAtSettle) -> float:
    """Velikost gammy expirace: hrubá z profilu, jinak |NetGEX| (starší partice bez profilu)."""
    return item.gross_gex if item.gross_gex is not None else abs(item.total_gex)


def build_cliff(
    session_date: dt.date, symbol: str, expiries: list[ExpiryAtSettle]
) -> CliffRecord | None:
    """Záznam útesu ze stavů sledovaných expirací k settle; None = nejde spočítat.

    Settlující expirace = ta s datem seance (0DTE). `gex_before` je Σ hrubé gammy
    (Σ|NetGEX| přes mřížku profilu; bez profilu |NetGEX| řetězu) přes všechny
    sledované expirace — poctivá poznámka: sweepujeme aktivní +
    následující expiraci (PR #94/#95), takže „všechny" znamená obě; vzdálenější
    řetězce nevidíme (odblokuje až M7 #616) a `cliff_share` je tím pádem horní
    odhad podílu.

    `wall_shift`: nové zdi = nejbližší PŘEŽIVŠÍ expirace (zbytkový profil známe
    ze sekundárního sweepu už před settle), staré = settlující řetěz.
    """
    settling_key = session_date.strftime("%Y%m%d")
    settling = next((item for item in expiries if item.expiry == settling_key), None)
    if settling is None:
        return None
    gex_before = sum(gex_magnitude(item) for item in expiries)
    gex_expiring = gex_magnitude(settling)
    survivors = sorted(
        (item for item in expiries if item.expiry != settling_key), key=lambda item: item.expiry
    )
    residual = survivors[0] if survivors else None
    return CliffRecord(
        session_date=session_date,
        symbol=symbol,
        gex_before=gex_before,
        gex_expiring=gex_expiring,
        cliff_share=gex_expiring / gex_before if gex_before > 0 else None,
        is_opex=is_opex_day(session_date),
        flip_shift=_shift(residual.flip if residual else None, settling.flip),
        call_wall_shift=_shift(residual.call_wall if residual else None, settling.call_wall),
        put_wall_shift=_shift(residual.put_wall if residual else None, settling.put_wall),
    )


def range_in_atr(
    session_range: float, previous_ranges: list[float], *, window: int = 14
) -> float | None:
    """Rozsah seance v násobcích průměrného rozsahu předchozích `window` seancí.

    „ATR" tady znamená SMA(high−low) seancí — bez gap složky true range;
    jednoduchá, reprodukovatelná definice pro korelace fáze 2.
    """
    recent = [value for value in previous_ranges[-window:] if value > 0]
    if not recent:
        return None
    average = sum(recent) / len(recent)
    if average <= 0:
        return None
    return session_range / average


def is_outside_band(depth: float) -> bool:
    """Minuta „mimo tlumící zónu" — TOTÉŽ pravidlo jako stínová brána #1060.

    `band_class` ∈ {outside, no_zone}, tj. hloubka ≤ 0 (pod hranou All; hrana
    sama je ještě outside). Žádný vlastní práh: vzorec hloubky pod hranou All
    je ve všech verzích metrik (v1–v3, #952/#1057) shodný, takže znaménko —
    a tím tato třída — na `band_metrics_version` nezávisí.
    """
    return band_class(depth) in (BAND_CLASS_OUTSIDE, BAND_CLASS_NO_ZONE)


def outside_share(depths: Sequence[float | None]) -> float | None:
    """Podíl minut mimo tlumící zónu (#1115, osa 2 fáze 2 #576).

    Jmenovatel = minuty se ZMĚŘENOU hloubkou (None = pásmo se v minutě
    nezměřilo, např. chybějící profil — nepočítá se ani do jmenovatele, aby
    díra v datech nevypadala jako „uvnitř zóny"). None = žádná změřená minuta.
    """
    measured = [depth for depth in depths if depth is not None]
    if not measured:
        return None
    outside = sum(1 for depth in measured if is_outside_band(depth))
    return outside / len(measured)
