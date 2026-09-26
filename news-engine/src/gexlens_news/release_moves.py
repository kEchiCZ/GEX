"""Velikost pohybu po ohlášeném releasu (#1296 fáze 3–4, ADR-0044) — čisté funkce.

Co se měří na shluku releasu × instrument (stejné funkce jako výzkum
`scripts/build_release_reactions.py`, aby se živá čísla dala porovnat):

* **výchylka za 15 min** (`reactions.measure_excursion`) — největší pohyb
  high/low proti close minuty před releasem, i když se cena vrátí;
* **výnos za 15 a 60 min** (`reactions.compute_reactions`) — close posledního
  baru v okně proti close minuty před releasem.

Měřitelné jen s barem **přesně v minutě před releasem** (zavřený trh ani díra
v datech se nezapočte — gap na otevření nepatří releasu) a s aspoň `h − 1` bary
v okně (jedna chybějící minuta se toleruje).

**Přepočet na dnešní volatilitu** (měření IS → OOS v ADR-0044): každý minulý
release dá **násobek** = výchylka / `vol_ref`, kde `vol_ref` = medián denního
rozsahu (high − low do settle, `gammacliff.session_ranges`, ADR-0028) 20 seancí
před seancí releasu dělený cenou před releasem. Očekávaná výchylka = medián
a p75 násobků × dnešní `vol_ref`. Doslovný „poměr mediánů“ (medián bp × vol
dnes / medián vol tehdy) vyšel hůř než žádné škálování.
"""

import datetime as dt
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from gexlens_news.reactions import Bar, compute_reactions, measure_excursion

#: Horizont výchylky v upozornění a v M1
MOVE_WINDOW_MIN = 15
#: Horizonty výnosu (H1: 15 min, H3: 60 min)
RETURN_WINDOWS = (15, 60)
#: Denní rozsahy pro `vol_ref`: 20 seancí, pod 15 se neurčuje
VOL_REF_SESSIONS = 20
VOL_REF_MIN_SESSIONS = 15
#: Pod tolik minulých releasů rodiny se očekávaná velikost neuvádí
MIN_HISTORY = 8

_MINUTE = dt.timedelta(minutes=1)


@dataclass(frozen=True)
class Measured:
    """Naměřené veličiny jednoho releasu na jednom instrumentu (None = nejde změřit)."""

    base_close: float | None
    exc_15m_bp: float | None
    ret_15m_bp: float | None
    ret_60m_bp: float | None


def measure_move(bars: Sequence[Bar], start: dt.datetime) -> Measured:
    """Výchylka 15 min a výnosy 15/60 min od minuty `start` (floor minuty releasu)."""
    ordered = sorted(bars, key=lambda bar: bar.ts)
    base = next((bar for bar in ordered if bar.ts == start - _MINUTE), None)
    if base is None or base.close <= 0:
        return Measured(base_close=None, exc_15m_bp=None, ret_15m_bp=None, ret_60m_bp=None)
    excursion = measure_excursion(ordered, start, MOVE_WINDOW_MIN)
    reactions = {
        reaction.window_min: reaction
        for reaction in compute_reactions(start, ordered, windows=RETURN_WINDOWS)
        if not reaction.deferred
    }
    returns: dict[int, float | None] = {}
    for window in RETURN_WINDOWS:
        end = start + dt.timedelta(minutes=window)
        in_window = sum(1 for bar in ordered if start <= bar.ts < end)
        reaction = reactions.get(window)
        returns[window] = (
            reaction.ret_bp if reaction is not None and in_window >= window - 1 else None
        )
    return Measured(
        base_close=base.close,
        exc_15m_bp=excursion.bp if excursion is not None else None,
        ret_15m_bp=returns[15],
        ret_60m_bp=returns[60],
    )


def vol_ref_bp(
    ranges: Sequence[tuple[dt.date, float]], session: dt.date, close: float | None
) -> float | None:
    """Medián denního rozsahu 20 seancí PŘED `session` / `close` v bp; None = málo dat."""
    if close is None or close <= 0:
        return None
    before = [value for day, value in ranges if day < session]
    window = before[-VOL_REF_SESSIONS:]
    if len(window) < VOL_REF_MIN_SESSIONS:
        return None
    return statistics.median(window) / close * 10_000


def _quartiles(values: Sequence[float]) -> tuple[float, float]:
    """Medián a p75 s lineární interpolací (jako výzkum; nearest-rank u n ≈ 25 skáče)."""
    _, median, upper = statistics.quantiles(values, n=4, method="inclusive")
    return median, upper


@dataclass(frozen=True)
class SizeStats:
    """Velikost výchylky za 15 min z minulých releasů rodiny na instrumentu."""

    #: Releasů s násobkem (výchylka i vol_ref)
    n: int
    mult_p50: float
    mult_p75: float
    #: Surové medián a p75 výchylky (bez přepočtu) a medián vol_ref týchž releasů
    raw_p50_bp: float
    raw_p75_bp: float
    vol_ref_median_bp: float

    def expected_bp(self, vol_now_bp: float) -> tuple[float, float]:
        """Očekávaná výchylka (medián, p75) přepočtená na dnešní volatilitu."""
        return self.mult_p50 * vol_now_bp, self.mult_p75 * vol_now_bp

    def vol_ratio(self, vol_now_bp: float) -> float:
        """Dnešní volatilita proti obvyklé při releasech rodiny."""
        return vol_now_bp / self.vol_ref_median_bp


def size_stats(rows: Sequence[tuple[float | None, float | None]]) -> SizeStats | None:
    """Statistika z dvojic (výchylka 15 min, vol_ref); None = pod `MIN_HISTORY` releasů."""
    pairs = [
        (exc, vol)
        for exc, vol in rows
        if exc is not None and vol is not None and vol > 0 and exc >= 0
    ]
    if len(pairs) < MIN_HISTORY:
        return None
    mult_p50, mult_p75 = _quartiles([exc / vol for exc, vol in pairs])
    raw_p50, raw_p75 = _quartiles([exc for exc, _ in pairs])
    return SizeStats(
        n=len(pairs),
        mult_p50=mult_p50,
        mult_p75=mult_p75,
        raw_p50_bp=raw_p50,
        raw_p75_bp=raw_p75,
        vol_ref_median_bp=statistics.median(vol for _, vol in pairs),
    )


def history_count(rows: Sequence[tuple[float | None, float | None]]) -> int:
    """Kolik releasů má výchylku i vol_ref (pro text „málo historie“)."""
    return sum(1 for exc, vol in rows if exc is not None and vol is not None and vol > 0)
