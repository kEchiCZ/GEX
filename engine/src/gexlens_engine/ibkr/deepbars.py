"""Hluboký backfill 1min barů z expirovaných kvartálních kontraktů (#369).

Reakce historických eventů (#277) potřebují bary starší než retenční okno.
`ContFuture` s `endDateTime` IBKR zakazuje (Error 10339 — a TWS při pokusu
zabije API socket, změřeno 29. 7.), takže se stahují **kvartální kontrakty
s `includeExpired`** — každý pro období, kdy byl front. Hloubka je omezená
IBKR na ~2 roky (starší kontrakty už nemají contract definition).

Hranice front oken jsou expirace (3. pátek kvartálního měsíce). Skutečný
volume roll probíhá ~týden před expirací — poslední dny okna tak nesou bary
dobíhajícího kontraktu s klesajícím objemem. Pro měření reakcí v bps je to
jedno (okna reakcí jsou minutová, basis rozdíl kontraktů se krátí).

Modul drží čisté plánování a bucketování (golden testy, pravidlo 3);
síťový runner je ve `scripts/backfill_bars.py`.
"""

import datetime as dt
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from gexlens_engine.ibkr.underlying import Bar

logger = logging.getLogger(__name__)

# Kvartální cyklus ES/NQ (H/M/U/Z)
QUARTER_MONTHS = (3, 6, 9, 12)

# durationStr "10 D" = 10 OBCHODNÍCH dní ≈ 12–14 kalendářních (změřeno);
# krok 12 kalendářních dní dává překryv, který řeší upsert dle ts_min
CHUNK_CALENDAR_DAYS = 12
CHUNK_DURATION = "10 D"


def quarterly_expiry(year: int, month: int) -> dt.date:
    """3. pátek kvartálního měsíce — expirace ES/NQ futures."""
    first = dt.date(year, month, 1)
    # Pátek = weekday 4; první pátek + 2 týdny
    first_friday = first + dt.timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + dt.timedelta(days=14)


def _quarterlies_until(last: dt.date) -> Iterable[tuple[str, dt.date]]:
    """(YYYYMM, expirace) kvartálních kontraktů chronologicky až za `last`."""
    year = last.year - 3
    while True:
        for month in QUARTER_MONTHS:
            expiry = quarterly_expiry(year, month)
            yield f"{year}{month:02d}", expiry
            if expiry > last:
                return
        year += 1


@dataclass(frozen=True)
class FrontWindow:
    """Období, kdy byl kontrakt front — bary se stahují z něj."""

    contract_month: str  # "202509"
    start: dt.date  # exkluzivně den po expiraci předchozího kvartálu
    end: dt.date  # inkluzivně (expirace, u aktuálního frontu dnešek)


def front_windows(depth_days: int, *, today: dt.date) -> list[FrontWindow]:
    """Front okna pokrývající [today − depth_days, včera].

    Dnešek se vynechává — aktuální den vlastní běžící engine (živý stream
    + jeho vlastní backfill); hluboký backfill do něj nesmí sahat.
    """
    horizon_start = today - dt.timedelta(days=depth_days)
    yesterday = today - dt.timedelta(days=1)
    windows: list[FrontWindow] = []
    previous_expiry: dt.date | None = None
    for contract_month, expiry in _quarterlies_until(yesterday):
        if previous_expiry is not None:
            window_start = previous_expiry + dt.timedelta(days=1)
        else:
            window_start = expiry - dt.timedelta(days=90)
        previous_expiry = expiry
        start = max(window_start, horizon_start)
        end = min(expiry, yesterday)
        if start > end:
            continue
        windows.append(FrontWindow(contract_month=contract_month, start=start, end=end))
    return windows


@dataclass(frozen=True)
class FetchTask:
    """Jeden reqHistoricalData request: chunk barů jednoho kontraktu."""

    symbol: str
    contract_month: str
    end: dt.date  # endDateTime = půlnoc UTC dne po `end`
    duration: str = CHUNK_DURATION

    @property
    def span_start(self) -> dt.date:
        """Nejstarší den, který chunk pokrývá (konzervativně kalendářně)."""
        return self.end - dt.timedelta(days=CHUNK_CALENDAR_DAYS - 1)


def chunk_tasks(symbol: str, window: FrontWindow) -> list[FetchTask]:
    """Chunky okna od nejstaršího konce k nejnovějšímu, s překryvem na hranách."""
    tasks: list[FetchTask] = []
    end = window.start + dt.timedelta(days=CHUNK_CALENDAR_DAYS - 1)
    while True:
        clamped = min(end, window.end)
        tasks.append(FetchTask(symbol=symbol, contract_month=window.contract_month, end=clamped))
        if clamped >= window.end:
            return tasks
        end = clamped + dt.timedelta(days=CHUNK_CALENDAR_DAYS)


def build_plan(symbols: Sequence[str], depth_days: int, *, today: dt.date) -> list[FetchTask]:
    """Plán všech requestů: symboly × front okna × chunky, chronologicky."""
    plan: list[FetchTask] = []
    for symbol in symbols:
        for window in front_windows(depth_days, today=today):
            plan.extend(chunk_tasks(symbol, window))
    return plan


def existing_days(derived_dir: Path, symbol: str) -> set[dt.date]:
    """Dny, které už mají partici barů — přeskakují se (idempotence)."""
    directory = derived_dir / symbol / "bars"
    if not directory.exists():
        return set()
    days: set[dt.date] = set()
    for path in directory.glob("*.parquet"):
        try:
            days.add(dt.date.fromisoformat(path.stem))
        except ValueError:
            logger.debug("Partice s nečitelným datem: %s", path)
    return days


def task_is_covered(task: FetchTask, existing: set[dt.date]) -> bool:
    """Chunk se přeskočí, když všechny jeho kalendářní dny už partici mají.

    Víkendové dny se nepočítají — bary pro ně nikdy nevzniknou (sobota) nebo
    vznikají až nedělním otevřením, které pokrývá pondělní obchodní den.
    """
    day = task.span_start
    while day <= task.end:
        if day.weekday() < 5 and day not in existing:
            return False
        day += dt.timedelta(days=1)
    return True


def bucket_by_day(bars: Iterable[Bar]) -> dict[dt.date, list[Bar]]:
    """Bary do denních partic podle UTC dne — tvar, který zapisuje write_bars."""
    buckets: dict[dt.date, list[Bar]] = {}
    for bar in bars:
        buckets.setdefault(bar.ts.astimezone(dt.UTC).date(), []).append(bar)
    return buckets
