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
from dataclasses import dataclass


@dataclass(frozen=True)
class ExpiryAtSettle:
    """Stav jedné sledované expirace v poslední minutě ≤ settle seance."""

    expiry: str  # YYYYMMDD
    total_gex: float  # NetGEX (se znaménkem)
    flip: float | None
    call_wall: float | None
    put_wall: float | None


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


def build_cliff(
    session_date: dt.date, symbol: str, expiries: list[ExpiryAtSettle]
) -> CliffRecord | None:
    """Záznam útesu ze stavů sledovaných expirací k settle; None = nejde spočítat.

    Settlující expirace = ta s datem seance (0DTE). `gex_before` je Σ|NetGEX|
    přes všechny sledované expirace — poctivá poznámka: sweepujeme aktivní +
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
    gex_before = sum(abs(item.total_gex) for item in expiries)
    gex_expiring = abs(settling.total_gex)
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
