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
síťový runner je ve `scripts/backfill_bars.py`. Týž runner umí i opravu
existujících dnů (#1320): doplnit chybějící minuty a nahradit rekonstrukci
`tasty_candle` barem IBKR historical — plán dne skládá `plan_day_refill`.
"""

import datetime as dt
import logging
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from gexlens_engine.compute.settle import QUARTER_MONTHS, is_trading_session, quarterly_expiry
from gexlens_engine.ibkr.underlying import BACKFILL_CONTRACT_TOLERANCE, Bar
from gexlens_engine.storage.parquet_store import BAR_SOURCE_RECONSTRUCTED

logger = logging.getLogger(__name__)

# Kvartální cyklus ES/NQ (H/M/U/Z)

# durationStr "10 D" = 10 OBCHODNÍCH dní ≈ 12–14 kalendářních (změřeno);
# krok 12 kalendářních dní dává překryv, který řeší upsert dle ts_min
CHUNK_CALENDAR_DAYS = 12
CHUNK_DURATION = "10 D"
_MINUTE = dt.timedelta(minutes=1)


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

    Dny bez obchodní seance se nepočítají — bary pro ně nikdy nevzniknou
    (sobota) nebo vznikají až nedělním otevřením, které pokrývá pondělní
    obchodní den (predikát `settle.is_trading_session`, #1309).
    """
    day = task.span_start
    while day <= task.end:
        if is_trading_session(day) and day not in existing:
            return False
        day += dt.timedelta(days=1)
    return True


def bucket_by_day(bars: Iterable[Bar]) -> dict[dt.date, list[Bar]]:
    """Bary do denních partic podle UTC dne — tvar, který zapisuje write_bars."""
    buckets: dict[dt.date, list[Bar]] = {}
    for bar in bars:
        buckets.setdefault(bar.ts.astimezone(dt.UTC).date(), []).append(bar)
    return buckets


def contract_candidates(day: dt.date) -> list[str]:
    """Kvartální kontrakty (YYYYMM), ze kterých mohl engine `day` měřit bary (#1320).

    Do #1189 sledoval engine nejbližší nepropadlý kontrakt (front podle
    expirace, jako `front_windows`), od #1189 přepíná na další kvartál už v CME
    roll date (ADR-0039). Kalendář tedy nerozhodne, který z nich pipeline v daný
    den opravdu měřila — rozhodnou měřené bary (`contract_mismatch`, #1232).
    Pořadí: front podle expirace, pak následující kvartál.
    """
    candidates: list[str] = []
    year = day.year
    while len(candidates) < 2:
        for month in QUARTER_MONTHS:
            if quarterly_expiry(year, month) >= day and len(candidates) < 2:
                candidates.append(f"{year}{month:02d}")
        year += 1
    return candidates


@dataclass(frozen=True)
class RejectedBlock:
    """Souvislý blok minut, který se nezapíše — kontrakt na okraji nesedí (#1320).

    `deviation` = největší odchylka po stranách bloku (None = na žádné straně
    měřená minuta, kontrakt nejde ověřit).
    """

    start: dt.datetime
    end: dt.datetime
    minutes: int
    deviation: float | None


@dataclass(frozen=True)
class DayRefill:
    """Plán opravy jedné denní partice z IBKR historical (#1320)."""

    bars: list[Bar]  # k zápisu, seřazené
    filled: int  # minuty, které v partici chyběly
    replaced: int  # minuty `tasty_candle` nahrazené barem IBKR historical
    tasty_left: int  # minuty `tasty_candle`, které v partici po zápisu zůstanou
    rejected: list[RejectedBlock]  # bloky, na jejichž okraji kontrakt nesedí


def _blocks(bars: Sequence[Bar]) -> list[list[Bar]]:
    """Seřazené bary rozdělené na souvislé bloky po sobě jdoucích minut."""
    blocks: list[list[Bar]] = []
    for bar in bars:
        if blocks and bar.ts - blocks[-1][-1].ts == _MINUTE:
            blocks[-1].append(bar)
        else:
            blocks.append([bar])
    return blocks


def _edge_deviations(
    block: Sequence[Bar],
    pairs: Sequence[dt.datetime],
    measured: Mapping[dt.datetime, float],
    incoming: Mapping[dt.datetime, Bar],
) -> list[float]:
    """Odchylka staženého kontraktu od měřeného po obou stranách bloku.

    `pairs` = seřazené minuty, které partice má změřené a stažený den je má
    také. Na každé straně bloku se vezme nejbližší z nich a porovná se close
    **téže** minuty — pohyb trhu se do porovnání nepřimíchá, takže na
    vzdálenosti od bloku nezáleží. Kořenový ticker přepíná kontrakt jen rollem
    dopředu (pinovaný kontrakt má vlastní ticker, ADR-0041), takže sedí-li obě
    strany, sedí i blok mezi nimi.
    Strana bez takové minuty (začátek dne, denní pauza) do výsledku nepřispěje.
    """
    deviations: list[float] = []
    before = bisect_left(pairs, block[0].ts)
    after = bisect_right(pairs, block[-1].ts)
    for index in (before - 1, after):
        if 0 <= index < len(pairs):
            ts = pairs[index]
            deviations.append(abs(incoming[ts].close - measured[ts]) / measured[ts])
    return deviations


def plan_day_refill(
    existing: Mapping[dt.datetime, str | None],
    incoming: Iterable[Bar],
    *,
    measured: Mapping[dt.datetime, float],
    replace_tasty: bool,
    tolerance: float = BACKFILL_CONTRACT_TOLERANCE,
) -> DayRefill:
    """Které bary IBKR historical zapsat do partice, která už existuje (#1320).

    `existing` = minuta → `source` z partice (NULL = živá cesta), `measured` =
    close měřených minut téže partice (`SnapshotWriter.measured_bar_closes`),
    `incoming` = bary IBKR historical téhož dne z jednoho kontraktu. Kandidát
    k zápisu je minuta, která v partici chybí, a s `replace_tasty` i minuta
    rekonstruovaná z dxFeed (`tasty_candle`). Měřenou minutu (`ibkr`, NULL) ani
    dřívější doplnění (`ibkr_hist`) plán nepřepíše; `SnapshotWriter.write_bars`
    (`bar_source_rank`) to pak hlídá ještě jednou. Časy jsou v UTC na obou stranách.

    Kontrakt dne vybírá medián přes celý den (`contract_mismatch`), a ten
    projde i partici, ve které engine během dne přepnul kontrakt (roll se
    projeví až novým discovery — restart, nová seance). Proto se kandidáti
    zapisují po souvislých blocích a každý blok musí po obou stranách sedět na
    nejbližší měřené minuty v toleranci `tolerance` (`_edge_deviations`); blok,
    který nesedí nebo nemá na žádné straně měřenou minutu, se nezapíše a vrátí
    se v `rejected`.
    """
    by_ts = {bar.ts: bar for bar in incoming}
    pairs = sorted(ts for ts, close in measured.items() if close > 0 and ts in by_ts)
    candidates = [
        bar
        for ts, bar in sorted(by_ts.items())
        if ts not in existing or (replace_tasty and existing[ts] == BAR_SOURCE_RECONSTRUCTED)
    ]
    bars: list[Bar] = []
    rejected: list[RejectedBlock] = []
    for block in _blocks(candidates):
        deviations = _edge_deviations(block, pairs, measured, by_ts)
        if not deviations or max(deviations) > tolerance:
            rejected.append(
                RejectedBlock(
                    start=block[0].ts,
                    end=block[-1].ts,
                    minutes=len(block),
                    deviation=max(deviations) if deviations else None,
                )
            )
            continue
        bars.extend(block)
    filled = sum(1 for bar in bars if bar.ts not in existing)
    covered = {bar.ts for bar in bars}
    tasty_left = sum(
        1
        for ts, source in existing.items()
        if source == BAR_SOURCE_RECONSTRUCTED and ts not in covered
    )
    return DayRefill(
        bars=bars,
        filled=filled,
        replaced=len(bars) - filled,
        tasty_left=tasty_left,
        rejected=rejected,
    )
