"""Retro přepočet `news_reactions` podle #1502 a přehodnocení predikcí.

Od #1502 (ADR-0043 dodatek 13. 11.) má reakce základ v close posledního uzavřeného baru
před zprávou, okno od minuty zprávy (V2), kontaminace hlídá měřený úsek od close základu
(a) a neodložená reakce se měří až minutu po konci okna. Fáze řádku změřená dřívějším
vzorcem (`computed_at_min` nebo `computed_at_daily` < `--cutoff`, tedy před startem
news-engine s #1502) se přeměří produkční cestou: minutová fáze `ReactionJob.measure_minute`
a `write_minute`, denní fáze (1–10 dní, vlastník 13. 11.) `ReactionJob.measure_daily`
a `write_daily`. Rozsah a: všechny řádky, jen symboly, které fázi měly. Potom se
přehodnotí `news_prediction_outcomes` všech oken (`OUTCOME_WINDOWS`, vlastník 13. 11.)
z nového výnosu stejnou funkcí jako `PredictionJob.evaluate` (`prediction_job.outcome_of`).
GEX režim (`regime_min`, `regime_daily`) se mění jen o změnu spotu (#1502: spot základu
místo close baru minuty zprávy): kde dnešní levels se spotem dřívějšího vzorce nezopakují
uložený režim (retence levels 90 dní), zůstává uložený — nejde znovu pořídit. Okno, které
retro nově uzná za čisté, outcome nemá; doplní ho `PredictionJob.evaluate` v příštím cyklu.
Okno nově kontaminované outcome drží, váhy ho ale čtou podle reakce (#1708). Okno, které
nový úsek nezměří (bez barů, např. zpráva těsně před otevřením), dostane NULL jako u měření
jobem a jeho outcome se smaže (vlastník 13. 11.: a) — stejný stav jako u nové zprávy, kde
`PredictionJob.evaluate` okno bez výnosu nevyhodnotí. Váhy
`news_weights` přepočítá news-engine v příštím cyklu reakcí (à 300 s), `news_model_stats`
první cyklus po půlnoci UTC (`ModelStatsJob`).

**Rychlá cesta:** bary eventu z úzkého výřezu dne (základ … konec okna) místo
`load_range` přes ±5 dní (`NarrowBars`). Každý `--check-every`-tý event se změří i plnou
cestou (`CachedBars.load_range`) a výsledek se musí shodovat, jinak skript skončí chybou.
Baseline objemu (`vol_z`) je k obchodnímu dni eventu jako v doměření #1494. Denní
agregáty seancí (`ReactionJob.daily_sessions`) se načtou jednou za běh, až je potřeba.

**Idempotence a kroky:** kandidát je event s fází řádku změřenou před `--cutoff`;
přeměřená fáze dostane `computed_at_*` = čas běhu, takže ji další běh nevezme. Outcomes
eventů dávky se po zápisu přehodnotí podle stavu (uložený výnos fáze s `computed_at_*`
≥ `--cutoff`) a zapisují se jen rozdíly. Běh, který dojde na konec („hotovo“), projde
podle stavu i všechny outcomes (dávka přerušená mezi zápisem reakcí a outcomes). Běh končí
po `--max-minutes` (výchozí 9) nebo když volná paměť klesne pod `--min-available-mb`;
další spuštění pokračuje, přerušená závěrečná kontrola s `--sweep-from`. Dry-run nic
nezapisuje, a proto vypíše, odkud pokračovat (`--since`).

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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from reclassify_news_rules import database_url, make_engine  # noqa: E402
from sqlalchemy import and_, bindparam, delete, exists, func, or_, select, update  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from gexlens_engine.compute.settle import trading_session_date  # noqa: E402
from gexlens_engine.storage.sentiment import (  # noqa: E402
    REACTION_ALL_WINDOWS,
    REACTION_DAILY_WINDOWS,
    REACTION_WINDOWS,
    news_events,
    news_prediction_outcomes,
    news_predictions,
    news_reactions,
    outcome_reaction_onclause,
    reaction_phase,
)
from gexlens_news.bars import BarsRepository, CachedBars  # noqa: E402
from gexlens_news.prediction_job import outcome_of  # noqa: E402
from gexlens_news.reaction_job import (  # noqa: E402
    CLOSURE_LOOKAHEAD_DAYS,
    CLOSURE_LOOKBACK_DAYS,
    LevelsRegimeReader,
    MinuteMeasurement,
    PhaseRow,
    ReactionJob,
)
from gexlens_news.reactions import (  # noqa: E402
    DEFAULT_WINDOWS,
    DEFERRED_GAP_MINUTES,
    Bar,
    SessionDaily,
)

SYMBOLS = ("ES", "NQ")
PHASES = ("min", "daily")
#: Okna outcomes, která se přehodnotí (vlastník 13. 11.: všechna přeměřená okna)
OUTCOME_WINDOWS = REACTION_ALL_WINDOWS
#: Okno, jehož otočené znaménko report počítá: minutová fáze 5 min, denní 1 den
FLIP_WINDOW = {"min": 5, "daily": 1440}
#: Okna a sloupce okna fáze širokého řádku (`vol_z` jen minutová, #998)
PHASE_WINDOWS = {"min": REACTION_WINDOWS, "daily": REACTION_DAILY_WINDOWS}
WINDOW_COLUMNS = {"min": ("ret", "range", "vol_z", "cont"), "daily": ("ret", "range", "cont")}
DEFAULT_BATCH = 200
DEFAULT_CHECK_EVERY = 97
DEFAULT_MAX_MINUTES = 9.0
DEFAULT_MIN_AVAILABLE_MB = 600.0
#: Predikce na jeden dotaz závěrečné kontroly outcomes (≤ 16 outcomes na predikci)
SWEEP_PREDICTIONS = 5000
#: Rozsahy `ReactionJob`: minutová fáze load_range(t − 5 d, t + (max + 1) min + 5 d),
#: denní fáze load_range(t − 5 d, t + 5 d)
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

    def daily_slice(self, t: dt.datetime) -> list[Bar]:
        """Výřez se stejným výsledkem `compute_daily_reactions` jako `load_range(t − 5 d, t + 5 d)`.

        Denní okna berou z barů jen základ (nejvýš 2 bary před prvním barem ≥ t) a první
        obchodovaný bar po zprávě; zbytek je z denních agregátů seancí.
        """
        ts = self.ts
        lo = bisect.bisect_left(ts, t - LOOKBACK)
        hi = bisect.bisect_right(ts, t + LOOKAHEAD)
        k = bisect.bisect_left(ts, t)
        return self.bars[max(lo, k - 3) : min(hi, k + 1)]


class NarrowBars(BarsRepository):
    """`load_range` pro `ReactionJob.measure_minute` a `measure_daily` z úzkého výřezu.

    Jen pro měření oken; baseline objemu (`recent_sessions` přes `load_range`) se počítá
    nad `CachedBars`. Jiný rozsah než ten z měření `ReactionJob` je chyba volajícího.
    """

    def __init__(self, cached: CachedBars) -> None:
        super().__init__(cached.data_dir)
        self._cached = cached
        self._windows: dict[str, DayWindow] = {}

    def load_day(self, symbol: str, day: dt.date) -> list[Bar]:
        return self._cached.load_day(symbol, day)

    def load_range(self, symbol: str, start: dt.datetime, end: dt.datetime) -> list[Bar]:
        t = start + LOOKBACK
        slicer: Callable[[DayWindow, dt.datetime], list[Bar]]
        if end == t + SPAN + LOOKAHEAD:
            slicer = DayWindow.minute_slice
        elif end == t + LOOKAHEAD:
            slicer = DayWindow.daily_slice
        else:
            raise ValueError(f"NarrowBars: nečekaný rozsah {start.isoformat()} … {end.isoformat()}")
        window = self._windows.get(symbol)
        if window is None:
            window = self._windows[symbol] = DayWindow(self._cached, symbol)
        window.ensure(t)
        return slicer(window, t)


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
    """Počty po fázích a měsících zprávy; klíč čítačů řádků je (fáze, měsíc)."""

    events: Counter[str] = field(default_factory=Counter)
    rows: Counter[tuple[str, str]] = field(default_factory=Counter)
    changed: Counter[tuple[str, str]] = field(default_factory=Counter)
    flipped: Counter[tuple[str, str]] = field(default_factory=Counter)
    cont_5: Counter[str] = field(default_factory=Counter)
    unmeasurable: Counter[str] = field(default_factory=Counter)
    regime_kept: Counter[str] = field(default_factory=Counter)
    regime_changed: Counter[str] = field(default_factory=Counter)
    vanished: Counter[str] = field(default_factory=Counter)
    checked: int = 0
    outcomes_seen: int = 0
    outcomes_flipped: Counter[int] = field(default_factory=Counter)
    outcomes_orphaned: int = 0
    outcomes_deleted: int = 0
    outcomes_written: int = 0
    sweep_ran: bool = False
    sweep_seen: int = 0
    sweep_orphaned: int = 0
    sweep_deleted: int = 0
    sweep_written: int = 0
    sweep_resume: int | None = None
    stopped: str = DONE
    resume_since: dt.datetime | None = None

    def render(self, *, apply: bool) -> str:
        verb = "přeměřeno" if apply else "k přeměření"
        lines = [
            f"| měsíc | eventů | min. řádků {verb} | se změnou | ret_5 jiné znaménko "
            f"| cont_5 jiné | denních řádků {verb} | se změnou | ret_1d jiné znaménko |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for month in sorted(self.events):
            lines.append(
                f"| {month} | {self.events[month]} | {self.rows['min', month]} "
                f"| {self.changed['min', month]} | {self.flipped['min', month]} "
                f"| {self.cont_5[month]} | {self.rows['daily', month]} "
                f"| {self.changed['daily', month]} | {self.flipped['daily', month]} |"
            )
        lines += ["", f"Celkem eventů {sum(self.events.values())}."]
        for phase, label in (("min", "Minutová fáze"), ("daily", "Denní fáze")):
            window = FLIP_WINDOW[phase]
            lines.append(
                f"{label}: řádků {self._total(self.rows, phase)}, se změnou "
                f"{self._total(self.changed, phase)}, ret_{window} jiné znaménko "
                f"{self._total(self.flipped, phase)}, teď nejde změřit (beze změny, zůstávají "
                f"kandidáty) {self.unmeasurable[phase]}, `regime_{phase}` jiný spotem základu "
                f"{self.regime_changed[phase]}, ponechaný uložený (dnešní levels ho nezopakují) "
                f"{self.regime_kept[phase]}, okno bez barů nového úseku (uložený výnos → NULL) "
                f"{self.vanished[phase]}."
            )
        flipped = ", ".join(
            f"{window}: {self.outcomes_flipped[window]}" for window in OUTCOME_WINDOWS
        )
        lines += [
            f"cont_5 jiné celkem {sum(self.cont_5.values())}.",
            f"Kontrola úzkého výřezu proti `load_range`: {self.checked} eventů, shoda.",
            f"Outcomes oken {OUTCOME_WINDOWS} s přeměřeným oknem: posouzeno "
            f"{self.outcomes_seen}, jiný směr nebo správnost "
            f"{sum(self.outcomes_flipped.values())} (po oknech {flipped}), zapsáno "
            f"{self.outcomes_written}; na okně bez výnosu {self.outcomes_orphaned}, "
            f"{'smazáno' if apply else 'ke smazání (dry-run nemaže)'} "
            f"{self.outcomes_deleted if apply else self.outcomes_orphaned}.",
        ]
        if self.sweep_ran:
            lines.append(
                f"Závěrečná kontrola outcomes podle stavu: posouzeno {self.sweep_seen}, "
                f"zapsáno {self.sweep_written}, na okně bez výnosu {self.sweep_orphaned}, "
                f"smazáno {self.sweep_deleted}."
            )
        lines.append(f"Konec běhu: {self.stopped}.")
        if self.resume_since is not None:
            lines.append(f"Pokračování dry-runu: `--since {self.resume_since.isoformat()}`.")
        return "\n".join(lines)

    @staticmethod
    def _total(counter: Counter[tuple[str, str]], phase: str) -> int:
        return sum(count for (key, _), count in counter.items() if key == phase)


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


def _stale(computed_at: dt.datetime | None, cutoff: dt.datetime) -> bool:
    return computed_at is not None and _utc(computed_at) < cutoff


def _old_spot(bars: Sequence[Bar], ts_event: dt.datetime) -> float | None:
    """Spot režimu dřívějšího vzorce: close posledního baru s `ts ≤ zpráva` (bar minuty zprávy)."""
    spot: float | None = None
    for bar in bars:
        if bar.ts <= ts_event:
            spot = float(bar.close)
        else:
            break
    return spot


def retro_regime(
    reader: LevelsRegimeReader,
    symbol: str,
    ts_event: dt.datetime,
    bars: Sequence[Bar],
    *,
    stored: str | None,
    measured: str | None,
) -> str | None:
    """Režim po retro: nový (spot základu) jen tam, kde dnešní levels zopakují uložený
    režim se spotem dřívějšího vzorce; jinak uložený (levels mimo retenci, přepsané levels)."""
    if stored is None or reader.regime_at(symbol, ts_event, _old_spot(bars, ts_event)) != stored:
        return stored
    return measured


def _same_rows(a: Sequence[PhaseRow] | None, b: Sequence[PhaseRow] | None) -> bool:
    if a is None or b is None:
        return a is b
    if len(a) != len(b):
        return False
    for left, right in zip(a, b, strict=True):
        if left.symbol != right.symbol or left.windows != right.windows:
            return False
        if left.values.keys() != right.values.keys():
            return False
        if not all(_same(left.values[k], right.values[k]) for k in left.values):
            return False
    return True


def _same_measurement(a: MinuteMeasurement, b: MinuteMeasurement) -> bool:
    return a.closed_flags == b.closed_flags and _same_rows(a.rows, b.rows)


def _stale_phase(computed_at: Any, cutoff: dt.datetime) -> Any:
    """SQL: fáze řádku je změřená a před `cutoff` (sloupec `computed_at_*`)."""
    return and_(computed_at.is_not(None), computed_at < cutoff)


def candidate_events(
    engine: Engine,
    cutoff: dt.datetime,
    *,
    since: dt.datetime | None,
    until: dt.datetime | None,
    after: tuple[dt.datetime, int] | None,
    limit: int,
) -> list[Any]:
    """Další dávka eventů s fází řádku změřenou před `cutoff`, podle času."""
    stale = exists().where(
        news_reactions.c.event_id == news_events.c.id,
        or_(
            _stale_phase(news_reactions.c.computed_at_min, cutoff),
            _stale_phase(news_reactions.c.computed_at_daily, cutoff),
        ),
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
    """Uložené řádky s fází změřenou před `cutoff`: event → symbol → sloupce."""
    stmt = select(news_reactions).where(
        news_reactions.c.event_id.in_(list(event_ids)),
        or_(
            _stale_phase(news_reactions.c.computed_at_min, cutoff),
            _stale_phase(news_reactions.c.computed_at_daily, cutoff),
        ),
    )
    found: dict[int, dict[str, Any]] = {}
    with engine.connect() as conn:
        for row in conn.execute(stmt).mappings():
            found.setdefault(int(row["event_id"]), {})[str(row["symbol"])] = dict(row)
    return found


def compare_phase(
    report: Report,
    phase: str,
    month: str,
    old_rows: Mapping[str, Mapping[str, Any]],
    measured: Sequence[PhaseRow],
    bars: BarsRepository,
    ahead: dt.timedelta,
    regimes: LevelsRegimeReader,
    ts_event: dt.datetime,
) -> list[PhaseRow]:
    """Porovná přeměřenou fázi s uloženou, doplní uložený režim; vrací řádky k zápisu.

    `bars` a `ahead` = výřez fáze (`load_range(t − 5 d, t + ahead)`) pro spot dřívějšího vzorce.
    Okno, které nový úsek nezměří (bez barů), dostane NULL ve všech sloupcích okna jako
    u měření jobem — UPDATE by jinak v řádku nechal hodnotu dřívějšího vzorce.
    """
    by_symbol = {row.symbol: row for row in measured}
    regime_key = f"regime_{phase}"
    flip = f"ret_{FLIP_WINDOW[phase]}"
    writable: list[PhaseRow] = []
    for symbol, old in old_rows.items():
        row = by_symbol.get(symbol)
        if row is None:
            report.unmeasurable[phase] += 1
            continue
        report.rows[phase, month] += 1
        new_regime = row.values.get(regime_key)
        kept = retro_regime(
            regimes,
            symbol,
            ts_event,
            bars.load_range(symbol, ts_event - LOOKBACK, ts_event + ahead),
            stored=old.get(regime_key),
            measured=new_regime if isinstance(new_regime, str) else None,
        )
        if kept != new_regime:
            report.regime_kept[phase] += 1
        elif kept != old.get(regime_key):
            report.regime_changed[phase] += 1
        row.values[regime_key] = kept
        for window in PHASE_WINDOWS[phase]:
            if f"ret_{window}" in row.values:
                continue
            if old.get(f"ret_{window}") is not None:
                report.vanished[phase] += 1
            row.values.update({f"{column}_{window}": None for column in WINDOW_COLUMNS[phase]})
        keys = [key for key in row.values if key != f"computed_at_{phase}"]
        if not all(_same(old.get(key), row.values[key]) for key in keys):
            report.changed[phase, month] += 1
        if _sign(old.get(flip)) != _sign(row.values.get(flip)):
            report.flipped[phase, month] += 1
        if phase == "min" and old.get("cont_5") != row.values.get("cont_5"):
            report.cont_5[month] += 1
        writable.append(row)
    return writable


def outcome_rows(
    engine: Engine,
    *,
    event_ids: Sequence[int] | None = None,
    cutoff: dt.datetime | None = None,
    predictions: tuple[int, int] | None = None,
) -> list[Any]:
    """Outcomes oken `OUTCOME_WINDOWS` s predikovaným směrem a eventem.

    `event_ids` = jen tyto eventy, `predictions` = jen predikce s id v [od, do),
    `cutoff` = jen reakce s fází přeměřenou od `cutoff` (přehodnocení podle stavu).
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
        joined = joined.join(news_reactions, outcome_reaction_onclause())
        columns += [news_reactions.c[f"ret_{window}"] for window in OUTCOME_WINDOWS]
        columns += [news_reactions.c.computed_at_min, news_reactions.c.computed_at_daily]
    stmt = (
        select(*columns)
        .select_from(joined)
        .where(news_prediction_outcomes.c.window_min.in_(OUTCOME_WINDOWS))
    )
    if event_ids is not None:
        stmt = stmt.where(news_predictions.c.event_id.in_(list(event_ids)))
    if predictions is not None:
        stmt = stmt.where(
            news_prediction_outcomes.c.prediction_id >= predictions[0],
            news_prediction_outcomes.c.prediction_id < predictions[1],
        )
    if cutoff is not None:
        stmt = stmt.where(
            or_(
                news_reactions.c.computed_at_min >= cutoff,
                news_reactions.c.computed_at_daily >= cutoff,
            )
        )
    with engine.connect() as conn:
        return list(conn.execute(stmt).fetchall())


def current_returns(
    rows: Iterable[Any], cutoff: dt.datetime
) -> dict[tuple[int, str, int], float | None]:
    """Výnos okna každého outcome z reakce, jejíž fáze okna je přeměřená od `cutoff`.

    None = přeměřená fáze okno nezměřila (bez barů nového úseku).
    """
    returns: dict[tuple[int, str, int], float | None] = {}
    for row in rows:
        window = int(row.window_min)
        measured = getattr(row, f"computed_at_{reaction_phase(window)}")
        if measured is None or _utc(measured) < cutoff:
            continue
        value = getattr(row, f"ret_{window}")
        returns[(int(row.event_id), str(row.symbol), window)] = (
            float(value) if value is not None else None
        )
    return returns


@dataclass
class Assessment:
    """Outcomes s přeměřeným oknem: posouzené, na okně bez výnosu (ke smazání) a změny."""

    seen: int = 0
    orphans: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)


def assess_outcomes(
    rows: Iterable[Any], returns: Mapping[tuple[int, str, int], float | None]
) -> Assessment:
    """Outcomes, kterým nový výnos okna dá jiný směr nebo správnost.

    Outcome okna, které ještě není přeměřené, se přeskočí. Outcome okna, které nový vzorec
    nezměřil, patří do `orphans` (smaže se, vlastník 13. 11.: a).
    """
    result = Assessment()
    for row in rows:
        key = (int(row.event_id), str(row.symbol), int(row.window_min))
        if key not in returns:
            continue
        ret = returns[key]
        if ret is None:
            result.orphans.append(
                {
                    "b_prediction": int(row.prediction_id),
                    "b_symbol": str(row.symbol),
                    "b_window": int(row.window_min),
                }
            )
            continue
        result.seen += 1
        realized, correct = outcome_of(int(row.predicted_dir), ret)
        if realized != row.realized_dir or correct != bool(row.correct):
            result.updates.append(
                {
                    "b_prediction": int(row.prediction_id),
                    "b_symbol": str(row.symbol),
                    "b_window": int(row.window_min),
                    "b_realized": realized,
                    "b_correct": correct,
                }
            )
    return result


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


def delete_outcomes(engine: Engine, keys: Sequence[dict[str, Any]], *, batch: int) -> int:
    """Smaže outcomes podle klíče (predikce, symbol, okno) — jen ty z `Assessment.orphans`."""
    statement = delete(news_prediction_outcomes).where(
        news_prediction_outcomes.c.prediction_id == bindparam("b_prediction"),
        news_prediction_outcomes.c.symbol == bindparam("b_symbol"),
        news_prediction_outcomes.c.window_min == bindparam("b_window"),
    )
    for start in range(0, len(keys), batch):
        with engine.begin() as conn:
            conn.execute(statement, list(keys[start : start + batch]))
    return len(keys)


def reevaluate_outcomes(
    engine: Engine,
    cutoff: dt.datetime,
    *,
    batch: int,
    event_ids: Sequence[int] | None = None,
    predictions: tuple[int, int] | None = None,
) -> tuple[Assessment, int, int]:
    """Přehodnocení podle stavu (idempotentní): posouzení, zapsané a smazané outcomes."""
    rows = outcome_rows(engine, event_ids=event_ids, cutoff=cutoff, predictions=predictions)
    assessment = assess_outcomes(rows, current_returns(rows, cutoff))
    written = write_outcomes(engine, assessment.updates, batch=batch)
    return assessment, written, delete_outcomes(engine, assessment.orphans, batch=batch)


def sweep_outcomes(
    engine: Engine,
    cutoff: dt.datetime,
    report: Report,
    *,
    batch: int,
    start: int | None,
    limit_reason: Callable[[], str | None],
) -> None:
    """Závěrečná kontrola všech outcomes podle stavu, po rozsazích id predikcí.

    Hlídá tytéž limity jako přeměření; přerušená kontrola pokračuje `--sweep-from`.
    """
    report.sweep_ran = True
    with engine.connect() as conn:
        lowest, highest = conn.execute(
            select(func.min(news_predictions.c.id), func.max(news_predictions.c.id))
        ).one()
    if lowest is None:
        return
    first = max(int(lowest), start) if start is not None else int(lowest)
    while first <= int(highest):
        reason = limit_reason()
        if reason is not None:
            report.sweep_resume = first
            report.stopped = f"{reason} — spusť znovu s `--sweep-from {first}`"
            return
        assessment, written, deleted = reevaluate_outcomes(
            engine, cutoff, batch=batch, predictions=(first, first + SWEEP_PREDICTIONS)
        )
        report.sweep_seen += assessment.seen
        report.sweep_orphaned += len(assessment.orphans)
        report.sweep_written += written
        report.sweep_deleted += deleted
        first += SWEEP_PREDICTIONS


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
    sweep_from: int | None = None,
    log: Any = print,
) -> Report:
    if cutoff > now:
        raise SystemExit(f"--cutoff {cutoff.isoformat()} je v budoucnosti")
    cached = CachedBars(data_dir)
    narrow = NarrowBars(cached)
    job = ReactionJob(engine, cached, symbols=SYMBOLS)
    regimes = LevelsRegimeReader(data_dir)
    report = Report()
    began = time.monotonic()

    def limit_reason() -> str | None:
        if (time.monotonic() - began) / 60 >= max_minutes:
            return f"časový limit {max_minutes:g} min"
        free = available_mb()
        if free is not None and free < min_available_mb:
            return f"volná paměť {free:.0f} MB pod {min_available_mb:g} MB"
        return None

    after: tuple[dt.datetime, int] | None = None
    baseline_day: dt.date | None = None
    baselines: dict[str, Any] = {}
    series: dict[str, list[SessionDaily]] | None = None
    measured_events = 0
    while True:
        events = candidate_events(
            engine, cutoff, since=since, until=until, after=after, limit=batch
        )
        if not events:
            break
        event_ids = [int(e.id) for e in events]
        stored = stored_rows(engine, event_ids, cutoff)
        new_returns: dict[tuple[int, str, int], float | None] = {}
        for event in events:
            event_id, ts_event = int(event.id), _utc(event.ts_event)
            old_rows = stored.get(event_id, {})
            month = ts_event.strftime("%Y-%m")
            report.events[month] += 1
            measured_events += 1
            check = bool(check_every) and measured_events % check_every == 0
            checked = False
            written: dict[str, list[PhaseRow]] = {}
            stale_min = {s: r for s, r in old_rows.items() if _stale(r["computed_at_min"], cutoff)}
            if stale_min:
                day = trading_session_date(ts_event)
                if day != baseline_day:
                    baselines = {s: job.baseline_for(s, day, bars=cached) for s in SYMBOLS}
                    baseline_day = day
                measurement = job.measure_minute(
                    ts_event, event.category, baselines, now, bars=narrow
                )
                if check:
                    full = job.measure_minute(ts_event, event.category, baselines, now, bars=cached)
                    if not _same_measurement(measurement, full):
                        raise SystemExit(f"Úzký výřez ≠ load_range (minuty): event {event_id}")
                    checked = True
                written["min"] = compare_phase(
                    report,
                    "min",
                    month,
                    stale_min,
                    measurement.rows,
                    narrow,
                    SPAN + LOOKAHEAD,
                    regimes,
                    ts_event,
                )
                if apply and written["min"]:
                    symbols = {row.symbol for row in written["min"]}
                    job.write_minute(event_id, measurement, only_symbols=symbols)
            stale_daily = {
                s: r for s, r in old_rows.items() if _stale(r["computed_at_daily"], cutoff)
            }
            if stale_daily:
                if series is None:
                    series = {symbol: job.daily_sessions(symbol) for symbol in SYMBOLS}
                daily = job.measure_daily(ts_event, series, now, bars=narrow)
                if check:
                    full_daily = job.measure_daily(ts_event, series, now, bars=cached)
                    if not _same_rows(daily, full_daily):
                        raise SystemExit(f"Úzký výřez ≠ load_range (dny): event {event_id}")
                    checked = True
                written["daily"] = compare_phase(
                    report,
                    "daily",
                    month,
                    stale_daily,
                    daily or [],
                    narrow,
                    LOOKAHEAD,
                    regimes,
                    ts_event,
                )
                if apply and written["daily"]:
                    job.write_daily(event_id, written["daily"])
            report.checked += checked
            for phase, phase_rows in written.items():
                for row in phase_rows:
                    for window in PHASE_WINDOWS[phase]:
                        value = row.values[f"ret_{window}"]
                        new_returns[(event_id, row.symbol, window)] = (
                            float(value) if value is not None else None  # type: ignore[arg-type]
                        )
        if apply:
            assessment, count, deleted = reevaluate_outcomes(
                engine, cutoff, batch=batch, event_ids=event_ids
            )
            report.outcomes_written += count
            report.outcomes_deleted += deleted
        else:
            assessment = assess_outcomes(outcome_rows(engine, event_ids=event_ids), new_returns)
        report.outcomes_seen += assessment.seen
        report.outcomes_orphaned += len(assessment.orphans)
        report.outcomes_flipped.update(int(item["b_window"]) for item in assessment.updates)
        last = events[-1]
        after = (_utc(last.ts_event), int(last.id))
        elapsed = (time.monotonic() - began) / 60
        free = available_mb()
        log(
            f"… {measured_events} eventů do {after[0].isoformat()} ({elapsed:.1f} min"
            + (f", volno {free:.0f} MB)" if free is not None else ")")
        )
        reason = limit_reason()
        if reason is not None:
            report.stopped = f"{reason} — spusť znovu"
            break
    if not apply and after is not None and report.stopped != DONE:
        report.resume_since = after[0]
    if apply and report.stopped == DONE:
        sweep_outcomes(
            engine, cutoff, report, batch=batch, start=sweep_from, limit_reason=limit_reason
        )
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
        help="StartedAt news-engine s #1502 (ISO s pásmem): starší fáze řádků se přeměří",
    )
    parser.add_argument("--apply", action="store_true", help="zapsat (jinak dry-run jen čtením)")
    parser.add_argument("--since", type=_parse_ts, help="jen zprávy s ts_event ≥ (ISO s pásmem)")
    parser.add_argument("--until", type=_parse_ts, help="jen zprávy s ts_event < (ISO s pásmem)")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--check-every", type=int, default=DEFAULT_CHECK_EVERY)
    parser.add_argument("--max-minutes", type=float, default=DEFAULT_MAX_MINUTES)
    parser.add_argument("--min-available-mb", type=float, default=DEFAULT_MIN_AVAILABLE_MB)
    parser.add_argument(
        "--sweep-from",
        type=int,
        help="id predikce, od kterého pokračuje přerušená závěrečná kontrola outcomes",
    )
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
        sweep_from=args.sweep_from,
    )
    mode = "zápis" if args.apply else "dry-run"
    print(f"`as_of` = {now.isoformat()}, cutoff {args.cutoff.isoformat()}, režim {mode}\n")
    print(report.render(apply=args.apply))


if __name__ == "__main__":
    main()
