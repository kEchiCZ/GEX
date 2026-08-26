"""Respektování pásma expected move (#872, D3 volatilitní vlny) — čisté funkce.

EM dne = mid(call) + mid(put) ATM straddlu v referenční minutě (zrcadlo
frontend `instrument/expectedmove.ts`, #676): první minuta US seance
s validním straddlem; hranice dne = kotva ± EM. Po settle se seance
klasifikuje: skončil close uvnitř pásma? Dotkl se rozsah hranice?

Proč to měřit: EM linie v grafu je bez kalibrace jen čára. „Close uvnitř
EM v X % dnů (teorie ~68 %)" z ní dělá nástroj — říká, jak moc věřit
fade od hranice pásma a jak výjimečný je průraz. Ukládá se i podíl minut
v negativní gammě: hypotéza z manuálu kap. 18 („průrazy se koncentrují
do negativní gammy") se pak doloží přímo nad touto tabulkou (#876).

Zdroje EM v pořadí: snapshoty 0DTE řetězu (mid kotace, retence 14 dní),
fallback close prémie z věčného `oi_eod` (#519) — včerejší závěrečný
straddle jako pre-open odhad. Zdroj se ukládá: obě čísla NEJSOU totéž
a míchat je do jedné statistiky bez označení by kalibraci falšovalo.
"""

import datetime as dt
from dataclasses import dataclass

#: Verze definice — hranice i zdroje se můžou kalibrovat, staré záznamy
#: musí nést, podle čeho tehdy vznikly (vzor ADR-0028).
EM_RESPECT_VERSION = 1

#: Kolik nejbližších strikes se zkouší, když ATM nemá obě strany
#: (zrcadlo MAX_ATM_CANDIDATES z frontend expectedmove.ts).
MAX_ATM_CANDIDATES = 3

#: Zdroje EM referenčního bodu.
SOURCE_STRADDLE = "straddle"
SOURCE_CLOSE_PREM = "close_prem"


@dataclass(frozen=True)
class StraddleQuote:
    """Jedna strana řetězu v referenční minutě — mid 0 = kotace chybí (#469)."""

    strike: float
    call_mid: float
    put_mid: float


@dataclass(frozen=True)
class EmReference:
    """Referenční bod pásma: kotva (spot), ATM strike a EM v bodech."""

    ts: dt.datetime | None
    source: str
    anchor: float
    atm_strike: float
    em_points: float

    @property
    def upper(self) -> float:
        return self.anchor + self.em_points

    @property
    def lower(self) -> float:
        return self.anchor - self.em_points


@dataclass(frozen=True)
class EmRespect:
    """Klasifikace jedné seance vůči pásmu EM — řádek tabulky `em_respect`."""

    session_date: dt.date
    symbol: str
    reference: EmReference
    high: float
    low: float
    close: float
    close_in_band: bool
    touch_upper: bool
    touch_lower: bool
    #: (high − low) / EM; 2.0 = rozsah přesně vyplnil celé pásmo.
    range_vs_em: float
    #: Podíl měřených minut se spotem pod flipem (negativní gamma); None = bez levels.
    negative_gamma_share: float | None
    version: int = EM_RESPECT_VERSION

    @property
    def em_pct(self) -> float:
        """EM jako % kotvy — srovnatelné napříč instrumenty i časem."""
        return 100.0 * self.reference.em_points / self.reference.anchor


def straddle_em(quotes: list[StraddleQuote], spot: float) -> tuple[float, float] | None:
    """(ATM strike, EM) z kotací minuty — nejbližší strike se zaplacenýma oběma
    stranama, do `MAX_ATM_CANDIDATES` kroků od spotu (zrcadlo #676)."""
    candidates = sorted(
        (quote for quote in quotes if quote.call_mid > 0 and quote.put_mid > 0),
        key=lambda quote: abs(quote.strike - spot),
    )[:MAX_ATM_CANDIDATES]
    if not candidates:
        return None
    atm = candidates[0]
    return atm.strike, atm.call_mid + atm.put_mid


def classify(
    *,
    session_date: dt.date,
    symbol: str,
    reference: EmReference,
    high: float,
    low: float,
    close: float,
    negative_gamma_share: float | None,
) -> EmRespect | None:
    """Seance vůči pásmu; None při nesmyslném vstupu (EM ≤ 0 nebo prázdný rozsah).

    Hrany patří DOVNITŘ pásma: close přesně na hranici není průraz — stejná
    konvence jako flip zóna (#209), kde hrana patří do zóny.
    """
    if reference.em_points <= 0 or reference.anchor <= 0 or high < low:
        return None
    return EmRespect(
        session_date=session_date,
        symbol=symbol,
        reference=reference,
        high=high,
        low=low,
        close=close,
        close_in_band=reference.lower <= close <= reference.upper,
        touch_upper=high > reference.upper,
        touch_lower=low < reference.lower,
        range_vs_em=(high - low) / reference.em_points,
        negative_gamma_share=negative_gamma_share,
    )


def negative_share(
    spots: dict[dt.datetime, float], flips: dict[dt.datetime, float]
) -> float | None:
    """Podíl minut se spotem POD měřeným flipem; None bez společných minut.

    Hrana (spot == flip) se počítá jako ne-negativní — konzistentně s flip
    zónou (#209), kde hranice není negativní režim.
    """
    common = [ts for ts in spots if ts in flips]
    if not common:
        return None
    below = sum(1 for ts in common if spots[ts] < flips[ts])
    return below / len(common)
