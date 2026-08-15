"""Volatilitní režim z vlastních dat (ADR-0028, #713) — čisté funkce bez I/O.

Proč vůbec: bez volatilitního řezu se statistiky R průměrují přes
nesouměřitelné dny. ADR ES je ~30–40 bodů při VIX 12–15, ale 100+ nad VIX 30 —
**stejný stop v bodech je v jiném režimu úplně jiný obchod**.

Proč z barů a ne z opčního řetězce (ADR-0028):
- bary se nikdy nemažou a máme ~2 roky ES i NQ → percentily dávají smysl od
  prvního dne;
- analog VIX z vlastního řetězce sestavit NELZE: ES/NQ mají denní expirace
  a archiv drží 5 nejbližších ≈ týden, takže term structure neexistuje.

Proč percentil vlastní historie a ne absolutní práh: hodnoty VIX nejsou
přenositelné na ADR v bodech ani mezi instrumenty (medián ATR ES 1,57 b vs.
NQ 11,52 b). Percentil je jediné, co dá smysl pro oba.

POZOR na pojmenování: v repu jsou dvě různé věci pod jménem „ATR" —
`compute/setups.py::average_true_range` je skutečný true range VČETNĚ gapu,
`compute/gammacliff.py::range_in_atr` je SMA(high−low) BEZ gapu. Tenhle modul
pracuje výhradně s **denním rozsahem seance** (high−low) a slovo ATR
nepoužívá, aby nevznikl třetí význam.
"""

import datetime as dt
from dataclasses import dataclass

#: Verze definice režimu — ukládá se k záznamu, protože hranice se budou
#: kalibrovat a přeřazení starých záznamů by falšovalo historii.
VOL_REGIME_VERSION = 1

#: Kategorie podle percentilu v klouzavém okně.
VOL_BUCKETS = ("low", "normal", "elevated", "crisis")

#: Hranice percentilů mezi kategoriemi (0–1).
BUCKET_EDGES = (0.25, 0.60, 0.85)

#: Klouzavé okno a minimální vzorek. Pod minimem se režim NEURČUJE —
#: percentil z hrstky dnů by byl náhoda vydávaná za měření.
WINDOW_DAYS = 252
MIN_SAMPLE = 60


@dataclass(frozen=True)
class VolRegime:
    """Volatilitní režim jedné seance."""

    session_date: dt.date
    symbol: str
    #: Denní rozsah seance (high−low) v bodech podkladu.
    session_range: float
    #: Percentil rozsahu v klouzavém okně (0–1).
    percentile: float
    bucket: str
    #: Kolik historických seancí percentil počítal.
    sample: int
    version: int = VOL_REGIME_VERSION


def percentile_of(value: float, history: list[float]) -> float:
    """Podíl historických hodnot menších než `value` (0–1).

    Rovnost se počítá polovinou, aby opakované stejné rozsahy nespadly
    všechny do jedné krajní kategorie.
    """
    if not history:
        return 0.0
    below = sum(1 for item in history if item < value)
    equal = sum(1 for item in history if item == value)
    return (below + equal / 2) / len(history)


def bucket_for(percentile: float) -> str:
    """Kategorie z percentilu — hranice v `BUCKET_EDGES`."""
    for index, edge in enumerate(BUCKET_EDGES):
        if percentile < edge:
            return VOL_BUCKETS[index]
    return VOL_BUCKETS[-1]


def compute_regimes(
    ranges: list[tuple[dt.date, float]],
    symbol: str,
    *,
    window: int = WINDOW_DAYS,
    min_sample: int = MIN_SAMPLE,
) -> list[VolRegime]:
    """Režim pro každou seanci, která má dost historie PŘED sebou.

    Percentil se počítá jen z PŘEDCHOZÍCH seancí — zahrnout dnešek by byl
    look-ahead: den by se hodnotil proti sobě samému.
    """
    ordered = sorted(ranges)
    result: list[VolRegime] = []
    for index, (session, value) in enumerate(ordered):
        if value <= 0:
            continue
        history = [item for _, item in ordered[max(0, index - window) : index] if item > 0]
        if len(history) < min_sample:
            continue
        percentile = percentile_of(value, history)
        result.append(
            VolRegime(
                session_date=session,
                symbol=symbol,
                session_range=value,
                percentile=percentile,
                bucket=bucket_for(percentile),
                sample=len(history),
            )
        )
    return result
