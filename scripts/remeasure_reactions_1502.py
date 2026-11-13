"""Retro přepočet minutové fáze `news_reactions` podle #1502 a přehodnocení predikcí.

Od #1502 (ADR-0043 dodatek 13. 11.) má reakce základ v close posledního uzavřeného baru
před zprávou, okno od minuty zprávy (V2), kontaminace hlídá měřený úsek od close základu
(a) a neodložená reakce se měří až minutu po konci okna. Řádky změřené dřívějším vzorcem
(`computed_at_min` < `--cutoff`, tedy před startem news-engine s #1502) skript přeměří
produkční cestou `ReactionJob.measure_minute` a zapíše `ReactionJob.write_minute`
(rozsah a: všechny řádky s minutovou fází, jen symboly, které ji měly). Potom přehodnotí
`news_prediction_outcomes` oken `OUTCOME_WINDOWS` z nového výnosu stejnou funkcí jako
`PredictionJob.evaluate` (`prediction_job.outcome_of`, otázka 4 = a). Váhy `news_weights`
přepočítá news-engine v příštím cyklu reakcí (à 300 s), `news_model_stats` první cyklus
po půlnoci UTC (`ModelStatsJob`).

**Rychlá cesta:** bary eventu z úzkého výřezu dne (základ … konec okna) místo
`load_range(t − 5 d, t + 61 min + 5 d)` (`NarrowBars`). Každý `--check-every`-tý event se
změří i plnou cestou (`CachedBars.load_range`) a výsledek se musí shodovat, jinak skript
skončí chybou. Baseline objemu (`vol_z`) je k obchodnímu dni eventu jako v doměření #1494.

**Idempotence a kroky:** kandidát je event s řádkem `computed_at_min` < `--cutoff`;
přeměřený řádek dostane `computed_at_min` = čas běhu, takže ho další běh nevezme.
Přehodnocení outcomes porovnává stav (znaménko uloženého výnosu řádků s
`computed_at_min` ≥ `--cutoff`) a zapisuje jen rozdíly, na konci každého běhu se zápisem.
Běh končí po `--max-minutes` (výchozí 9) nebo když volná paměť klesne pod
`--min-available-mb`; další spuštění pokračuje. Dry-run nic nezapisuje, a proto vypíše,
odkud pokračovat (`--since`).

Použití z hostitele (PG produkce na :55432, heslo z `GEXLENS_PG_PASSWORD`, nevypisuje se):
    uv run python scripts/remeasure_reactions_1502.py --cutoff <StartedAt news-engine s #1502>
    pwsh scripts/backup-postgres.ps1
    uv run python scripts/remeasure_reactions_1502.py --cutoff <…> --apply   # opakovat do „hotovo“
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import math
import sys
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from reclassify_news_rules import database_url, make_engine  # noqa: E402
from sqlalchemy import bindparam, exists, select, update  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from gexlens_engine.compute.settle import trading_session_date  # noqa: E402
from gexlens_engine.storage.sentiment import (  # noqa: E402
    news_events,
    news_prediction_outcomes,
    news_predictions,
    news_reactions,
)
from gexlens_news.bars import BarsRepository, CachedBars  # noqa: E402
from gexlens_news.prediction_job import outcome_of  # noqa: E402
from gexlens_news.reaction_job import (  # noqa: E402
    CLOSURE_LOOKAHEAD_DAYS,
    CLOSURE_LOOKBACK_DAYS,
    MinuteMeasurement,
    ReactionJob,
)
from gexlens_news.reactions import DEFAULT_WINDOWS, DEFERRED_GAP_MINUTES, Bar  # noqa: E402

SYMBOLS = ("ES", "NQ")
#: Okna outcomes, která se přehodnotí (váhy `news_weights` čtou okno 5)
OUTCOME_WINDOWS = (5,)
DEFAULT_BATCH = 200
DEFAULT_CHECK_EVERY = 97
DEFAULT_MAX_MINUTES = 9.0
DEFAULT_MIN_AVAILABLE_MB = 600.0
#: Rozsah `ReactionJob.measure_minute`: load_range(t − 5 d, t + (max + 1) min + 5 d)
LOOKBACK = dt.timedelta(days=CLOSURE_LOOKBACK_DAYS)
LOOKAHEAD = dt.timedelta(days=CLOSURE_LOOKAHEAD_DAYS)
SPAN = dt.timedelta(minutes=max(DEFAULT_WINDOWS) + 1)
GAP = dt.timedelta(minutes=DEFERRED_GAP_MINUTES)
TOLERANCE = 1e-9
#: Konec běhu bez dalšího kandidáta
DONE = "hotovo, žádný další kandidát"


class DayWindow:
    """Bary symbolu pro dny čtené `load_range` kolem eventů dne D (D−6 … D+7), dedup #1002."""

    def __init__(self, days: BarsRepository, symbol: str) -> None:
        self._days = days
        self._symbol = symbol
        self._day: dt.date | None = None
        self.bars: list[Bar] = []
        self.ts: list[dt.datetime] = []

    def ensure(self, event_ts: dt.datetime) -> None:
        if event_ts.date() == self._day:
            return
        first = (event_ts - LOOKBACK).date() - dt.timedelta(days=1)
        last = (event_ts + SPAN + LOOKAHEAD).date() + dt.timedelta(days=2)
        # Stejné pravidlo jako `BarsRepository.load_range`: vyhrává partice UTC dne minuty
        by_minute: dict[dt.datetime, Bar] = {}
        day = first
        while day <= last:
            for bar in self._days.load_day(self._symbol, day):
                if bar.ts not in by_minute or bar.ts.date() == day:
                    by_minute[bar.ts] = bar
            day += dt.timedelta(days=1)
        self.bars = sorted(by_minute.values(), key=lambda bar: bar.ts)
        self.ts = [bar.ts for bar in self.bars]
        self._day = event_ts.date()

    def minute_slice(self, t: dt.datetime) -> list[Bar]:
        """Výřez se stejným výsledkem měření jako `load_range(t − 5 d, t + 61 min + 5 d)`.

        Obsahuje základ (poslední uzavřený bar před zprávou, nejvýš 2 bary před prvním
        barem ≥ t), první obchodovaný bar po zprávě (deferred) a celé okno od začátku.
        """
        ts = self.ts
        lo = bisect.bisect_left(ts, t - LOOKBACK)
        hi = bisect.bisect_right(ts, t + SPAN + LOOKAHEAD)
        k = bisect.bisect_left(ts, t)
        if k >= hi:
            return self.bars[max(lo, k - 3) : hi]
        first = ts[k]
        start = first if first - t >= GAP else t
        end = min(hi, bisect.bisect_left(ts, start + SPAN) + 2)
        return self.bars[max(lo, k - 3) : end]


class NarrowBars(BarsRepository):
    """`load_range` pro `ReactionJob.measure_minute` z úzkého výřezu (rychlá cesta).

    Jen pro měření oken; baseline objemu (`recent_sessions` přes `load_range`) se počítá
    nad `CachedBars`. Jiný rozsah než ten z `measure_minute` je chyba volajícího.
    """

    def __init__(self, cached: CachedBars) -> None:
        super().__init__(cached.data_dir)
        self._cached = cached
        self._windows: dict[str, DayWindow] = {}

    def load_day(self, symbol: str, day: dt.date) -> list[Bar]:
        return self._cached.load_day(symbol, day)

    def load_range(self, symbol: str, start: dt.datetime, end: dt.datetime) -> list[Bar]:
        t = start + LOOKBACK
        if end != t + SPAN + LOOKAHEAD:
            raise ValueError(f"NarrowBars: nečekaný rozsah {start.isoformat()} … {end.isoformat()}")
        window = self._windows.get(symbol)
        if window is None:
            window = self._windows[symbol] = DayWindow(self._cached, symbol)
        window.ensure(t)
        return window.minute_slice(t)


def available_mb() -> float | None:
    """Volná paměť hostitele v MB (Linux `/proc/meminfo`, Windows API); None = nezjištěno."""
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
        return None
    if sys.platform == "win32":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return float(status.ullAvailPhys) / 1024 / 1024
    return None


@dataclass
class Report:
    """Počty po měsících zprávy: eventy, řádky, změněné řádky, otočený směr a kontaminace okna 5."""

    events: Counter[str] = field(default_factory=Counter)
    rows: Counter[str] = field(default_factory=Counter)
    changed: Counter[str] = field(default_factory=Counter)
    flipped_5: Counter[str] = field(default_factory=Counter)
    cont_5: Counter[str] = field(default_factory=Counter)
    unmeasurable: int = 0
    checked: int = 0
    outcomes_seen: int = 0
    outcomes_flipped: int = 0
    outcomes_written: int = 0
    stopped: str = DONE
    resume_since: dt.datetime | None = None

    def render(self, *, apply: bool) -> str:
        verb = "přeměřeno" if apply else "k přeměření"
        lines = [
            f"| měsíc | eventů | řádků {verb} | se změnou | ret_5 jiné znaménko | cont_5 jiné |",
            "|---|---|---|---|---|---|",
        ]
        for month in sorted(self.events):
            lines.append(
                f"| {month} | {self.events[month]} | {self.rows[month]} | {self.changed[month]} "
                f"| {self.flipped_5[month]} | {self.cont_5[month]} |"
            )
        lines += [
            "",
            f"Celkem eventů {sum(self.events.values())}, řádků {sum(self.rows.values())}, "
            f"se změnou {sum(self.changed.values())}, ret_5 jiné znaménko "
            f"{sum(self.flipped_5.values())}, cont_5 jiné {sum(self.cont_5.values())}.",
            f"Řádků, které teď nejdou změřit (beze změny, zůstávají kandidáty): "
            f"{self.unmeasurable}.",
            f"Kontrola úzkého výřezu proti `load_range`: {self.checked} eventů, shoda.",
            f"Outcomes oken {OUTCOME_WINDOWS}: posouzeno {self.outcomes_seen}, jiný směr nebo "
            f"správnost {self.outcomes_flipped}, zapsáno {self.outcomes_written}.",
            f"Konec běhu: {self.stopped}.",
        ]
        if self.resume_since is not None:
            lines.append(f"Pokračování dry-runu: `--since {self.resume_since.isoformat()}`.")
        return "\n".join(lines)


def _same(old: object, new: object) -> bool:
    if old is None or new is None:
        return old is new
    if isinstance(old, float) or isinstance(new, float):
        return math.isclose(float(old), float(new), rel_tol=0.0, abs_tol=TOLERANCE)  # type: ignore[arg-type]
    if isinstance(old, dt.datetime) and isinstance(new, dt.datetime):
        return _utc(old) == _utc(new)
    return old == new


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _sign(value: object) -> int | None:
    if value is None:
        return None
    number = float(value)  # type: ignore[arg-type]
    return 1 if number > 0 else (-1 if number < 0 else 0)


def _same_measurement(a: MinuteMeasurement, b: MinuteMeasurement) -> bool:
    if a.closed_flags != b.closed_flags or len(a.rows) != len(b.rows):
        return False
    for left, right in zip(a.rows, b.rows, strict=True):
        if left.symbol != right.symbol or left.windows != right.windows:
            return False
        if left.values.keys() != right.values.keys():
            return False
        if not all(_same(left.values[k], right.values[k]) for k in left.values):
            return False
    return True


def candidate_events(
    engine: Engine,
    cutoff: dt.datetime,
    *,
    since: dt.datetime | None,
    until: dt.datetime | None,
    after: tuple[dt.datetime, int] | None,
    limit: int,
) -> list[Any]:
    """Další dávka eventů s řádkem minutové fáze změřeným před `cutoff`, podle času."""
    stale = exists().where(
        news_reactions.c.event_id == news_events.c.id,
        news_reactions.c.computed_at_min.is_not(None),
        news_reactions.c.computed_at_min < cutoff,
    )
    stmt = select(news_events.c.id, news_events.c.ts_event, news_events.c.category).where(stale)
    if since is not None:
        stmt = stmt.where(news_events.c.ts_event >= since)
    if until is not None:
        stmt = stmt.where(news_events.c.ts_event < until)
    if after is not None:
        ts, event_id = after
        stmt = stmt.where(
            (news_events.c.ts_event > ts)
            | ((news_events.c.ts_event == ts) & (news_events.c.id > event_id))
        )
    stmt = stmt.order_by(news_events.c.ts_event, news_events.c.id).limit(limit)
    with engine.connect() as conn:
        return list(conn.execute(stmt).fetchall())


def stored_rows(
    engine: Engine, event_ids: Sequence[int], cutoff: dt.datetime
) -> dict[int, dict[str, Any]]:
    """Uložené řádky minutové fáze změřené před `cutoff`: event → symbol → sloupce."""
    stmt = select(news_reactions).where(
        news_reactions.c.event_id.in_(list(event_ids)),
        news_reactions.c.computed_at_min.is_not(None),
        news_reactions.c.computed_at_min < cutoff,
    )
    found: dict[int, dict[str, Any]] = {}
    with engine.connect() as conn:
        for row in conn.execute(stmt).mappings():
            found.setdefault(int(row["event_id"]), {})[str(row["symbol"])] = dict(row)
    return found


def outcome_rows(
    engine: Engine, *, event_ids: Sequence[int] | None, cutoff: dt.datetime | None
) -> list[Any]:
    """Outcomes oken `OUTCOME_WINDOWS` s predikovaným směrem a eventem.

    `event_ids` = jen tyto eventy (dry-run), `cutoff` = jen reakce přeměřené od `cutoff`
    s uloženým výnosem okna (přehodnocení po zápisu).
    """
    columns = [
        news_prediction_outcomes.c.prediction_id,
        news_prediction_outcomes.c.symbol,
        news_prediction_outcomes.c.window_min,
        news_prediction_outcomes.c.realized_dir,
        news_prediction_outcomes.c.correct,
        news_predictions.c.predicted_dir,
        news_predictions.c.event_id,
    ]
    joined = news_prediction_outcomes.join(
        news_predictions, news_predictions.c.id == news_prediction_outcomes.c.prediction_id
    )
    if cutoff is not None:
        joined = joined.join(
            news_reactions,
            (news_reactions.c.event_id == news_predictions.c.event_id)
            & (news_reactions.c.symbol == news_prediction_outcomes.c.symbol),
        )
        columns += [news_reactions.c[f"ret_{window}"] for window in OUTCOME_WINDOWS]
    stmt = (
        select(*columns)
        .select_from(joined)
        .where(news_prediction_outcomes.c.window_min.in_(OUTCOME_WINDOWS))
    )
    if event_ids is not None:
        stmt = stmt.where(news_predictions.c.event_id.in_(list(event_ids)))
    if cutoff is not None:
        stmt = stmt.where(news_reactions.c.computed_at_min >= cutoff)
    with engine.connect() as conn:
        return list(conn.execute(stmt).fetchall())


def outcome_updates(
    rows: Iterable[Any], returns: dict[tuple[int, str, int], float]
) -> list[dict[str, Any]]:
    """Outcomes, kterým nový výnos okna dá jiný směr nebo správnost (`outcome_of`)."""
    updates = []
    for row in rows:
        ret = returns.get((int(row.event_id), str(row.symbol), int(row.window_min)))
        if ret is None:
            continue  # okno teď nejde změřit — outcome se nemění
        realized, correct = outcome_of(int(row.predicted_dir), float(ret))
        if realized != row.realized_dir or correct != bool(row.correct):
            updates.append(
                {
                    "b_prediction": int(row.prediction_id),
                    "b_symbol": str(row.symbol),
                    "b_window": int(row.window_min),
                    "b_realized": realized,
                    "b_correct": correct,
                }
            )
    return updates


def write_outcomes(engine: Engine, updates: Sequence[dict[str, Any]], *, batch: int) -> int:
    statement = (
        update(news_prediction_outcomes)
        .where(
            news_prediction_outcomes.c.prediction_id == bindparam("b_prediction"),
            news_prediction_outcomes.c.symbol == bindparam("b_symbol"),
            news_prediction_outcomes.c.window_min == bindparam("b_window"),
        )
        .values(realized_dir=bindparam("b_realized"), correct=bindparam("b_correct"))
    )
    for start in range(0, len(updates), batch):
        with engine.begin() as conn:
            conn.execute(statement, list(updates[start : start + batch]))
    return len(updates)


def reevaluate_outcomes(engine: Engine, cutoff: dt.datetime, report: Report, *, batch: int) -> None:
    """Přehodnocení podle stavu: outcomes reakcí přeměřených od `cutoff` (idempotentní)."""
    rows = outcome_rows(engine, event_ids=None, cutoff=cutoff)
    returns = {
        (int(row.event_id), str(row.symbol), window): float(value)
        for row in rows
        for window in OUTCOME_WINDOWS
        if (value := getattr(row, f"ret_{window}")) is not None
    }
    updates = outcome_updates(rows, returns)
    report.outcomes_seen += len(rows)
    report.outcomes_flipped += len(updates)
    report.outcomes_written += write_outcomes(engine, updates, batch=batch)


def run(
    engine: Engine,
    data_dir: Path,
    *,
    cutoff: dt.datetime,
    apply: bool,
    now: dt.datetime,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    batch: int = DEFAULT_BATCH,
    check_every: int = DEFAULT_CHECK_EVERY,
    max_minutes: float = DEFAULT_MAX_MINUTES,
    min_available_mb: float = DEFAULT_MIN_AVAILABLE_MB,
    log: Any = print,
) -> Report:
    if cutoff > now:
        raise SystemExit(f"--cutoff {cutoff.isoformat()} je v budoucnosti")
    cached = CachedBars(data_dir)
    narrow = NarrowBars(cached)
    job = ReactionJob(engine, cached, symbols=SYMBOLS)
    report = Report()
    began = time.monotonic()
    after: tuple[dt.datetime, int] | None = None
    baseline_day: dt.date | None = None
    baselines: dict[str, Any] = {}
    measured_events = 0
    while True:
        events = candidate_events(
            engine, cutoff, since=since, until=until, after=after, limit=batch
        )
        if not events:
            break
        stored = stored_rows(engine, [int(e.id) for e in events], cutoff)
        new_returns: dict[tuple[int, str, int], float] = {}
        for event in events:
            event_id, ts_event = int(event.id), _utc(event.ts_event)
            day = trading_session_date(ts_event)
            if day != baseline_day:
                baselines = {s: job.baseline_for(s, day, bars=cached) for s in SYMBOLS}
                baseline_day = day
            measurement = job.measure_minute(ts_event, event.category, baselines, now, bars=narrow)
            measured_events += 1
            if check_every and measured_events % check_every == 0:
                full = job.measure_minute(ts_event, event.category, baselines, now, bars=cached)
                if not _same_measurement(measurement, full):
                    raise SystemExit(f"Úzký výřez ≠ load_range: event {event_id}")
                report.checked += 1
            old_rows = stored.get(event_id, {})
            by_symbol = {row.symbol: row for row in measurement.rows}
            month = ts_event.strftime("%Y-%m")
            report.events[month] += 1
            for symbol, old in old_rows.items():
                row = by_symbol.get(symbol)
                if row is None:
                    report.unmeasurable += 1
                    continue
                report.rows[month] += 1
                keys = [key for key in row.values if key != "computed_at_min"]
                if not all(_same(old.get(key), row.values[key]) for key in keys):
                    report.changed[month] += 1
                if _sign(old.get("ret_5")) != _sign(row.values.get("ret_5")):
                    report.flipped_5[month] += 1
                if old.get("cont_5") != row.values.get("cont_5"):
                    report.cont_5[month] += 1
                for window in OUTCOME_WINDOWS:
                    value = row.values.get(f"ret_{window}")
                    if value is not None:
                        new_returns[(event_id, symbol, window)] = float(value)  # type: ignore[arg-type]
            writable = set(old_rows) & set(by_symbol)
            if apply and writable:
                job.write_minute(event_id, measurement, only_symbols=writable)
        if not apply:
            rows = outcome_rows(engine, event_ids=[int(e.id) for e in events], cutoff=None)
            report.outcomes_seen += len(rows)
            report.outcomes_flipped += len(outcome_updates(rows, new_returns))
        last = events[-1]
        after = (_utc(last.ts_event), int(last.id))
        elapsed = (time.monotonic() - began) / 60
        free = available_mb()
        log(
            f"… {measured_events} eventů do {after[0].isoformat()} ({elapsed:.1f} min"
            + (f", volno {free:.0f} MB)" if free is not None else ")")
        )
        if elapsed >= max_minutes:
            report.stopped = f"časový limit {max_minutes:g} min — spusť znovu"
            break
        if free is not None and free < min_available_mb:
            report.stopped = f"volná paměť {free:.0f} MB pod {min_available_mb:g} MB — spusť znovu"
            break
    if not apply and after is not None and report.stopped != DONE:
        report.resume_since = after[0]
    if apply:
        reevaluate_outcomes(engine, cutoff, report, batch=batch)
    return report


def _parse_ts(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(f"čas {value!r} bez pásma — zadej UTC, např. …Z")
    return parsed.astimezone(dt.UTC)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--db", help="SQLAlchemy URL; jinak z prostředí (nevypisuje se)")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data", help="kořen archivu")
    parser.add_argument(
        "--cutoff",
        type=_parse_ts,
        required=True,
        help="StartedAt news-engine s #1502 (ISO s pásmem): starší řádky minutové fáze se přeměří",
    )
    parser.add_argument("--apply", action="store_true", help="zapsat (jinak dry-run jen čtením)")
    parser.add_argument("--since", type=_parse_ts, help="jen zprávy s ts_event ≥ (ISO s pásmem)")
    parser.add_argument("--until", type=_parse_ts, help="jen zprávy s ts_event < (ISO s pásmem)")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--check-every", type=int, default=DEFAULT_CHECK_EVERY)
    parser.add_argument("--max-minutes", type=float, default=DEFAULT_MAX_MINUTES)
    parser.add_argument("--min-available-mb", type=float, default=DEFAULT_MIN_AVAILABLE_MB)
    args = parser.parse_args(argv)
    if not (args.data_dir / "derived").is_dir():
        raise SystemExit(f"Chybí archiv barů: {args.data_dir / 'derived'}")
    engine = make_engine(database_url(args.db), read_only=not args.apply)
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    report = run(
        engine,
        args.data_dir,
        cutoff=args.cutoff,
        apply=args.apply,
        now=now,
        since=args.since,
        until=args.until,
        batch=args.batch,
        check_every=args.check_every,
        max_minutes=args.max_minutes,
        min_available_mb=args.min_available_mb,
    )
    mode = "zápis" if args.apply else "dry-run"
    print(f"`as_of` = {now.isoformat()}, cutoff {args.cutoff.isoformat()}, režim {mode}\n")
    print(report.render(apply=args.apply))


if __name__ == "__main__":
    main()
