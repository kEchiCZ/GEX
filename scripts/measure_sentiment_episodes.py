"""Měření korekčních epizod SentIndexu: „pokus o korekci" vs. „negace" (#565 fáze 1).

Odpovídá na dvě otázky, nic nedetekuje živě ani neukládá (prod DB jen pro čtení):

1. Dá se z denní řady SentIndexu oddělit korekce, která se **zahladí** (pokus),
   od korekce, která **pokračuje** (negace) — a při jakém prahu D a horizontu H?
2. Říká to rozlišení něco o **ceně podkladu** (následný výnos ES/NQ), nebo jen o náladě?

Metodika (rev. 2 zadání #565, rozhodnutí #640 z 24. 8.):

* Řada = denní close SentIndexu (`sentiment_daily`) v jednotkách σ (`close_z`,
  σ = std předchozích 100 seancí, kauzálně, #640). Surová hodnota slouží jen pro
  MA10 (stejná definice jako `assess_state`, #563) — práh v surových jednotkách
  je kvůli érám měřítka bezcenný.
* **Epizoda A (drawdown)**: začíná dnem, kdy `close_z` klesne pod klouzavé
  20denní maximum o ≥ D σ. Další epizoda smí začít až po „odjištění" — dnu,
  kdy drawdown klesl pod D (jinak by negace zřetězila epizody den po dni).
* **Epizoda B (RiskOff)**: začíná dnem vstupu do stavu RiskOff podle
  `position_state` (#563); D se neuplatní.
* **Konec** = první den PO začátku, kdy `close_z` je zpět nad referenční úrovní
  (20denní maximum v den začátku) NEBO surový close nad MA10; jinak horizont H.
* **Třída**: pokus = zahlazeno do H obchodních dní podkladu; negace = ne.
  `depth_z` = největší pokles pod referenční úroveň během epizody (σ).
* **Následný výnos podkladu** (%, close-to-close v settle 16:00 ET, ADR-0023)
  na 5/10/20 obchodních dní. Kotva **den rozhodnutí** (den zahlazení, resp.
  start + H) = čistý forward test „třídu už známe, co udělá cena"; kotva
  **den začátku** je jen popisná (překrývá se s dobou, kdy třída ještě není známá).
  Cenzurované epizody (horizont za koncem dat) se NEextrapolují — vynechají se
  a započtou zvlášť.
* **Vyvážená přesnost** = ½·(P[výnos>0 | pokus] + P[výnos≤0 | negace]) — jak
  dobře třída odděluje znaménko následného výnosu (mapování pokus→růst).
  Hodnota < 0,5 znamená kontrariánské mapování (srov. #563).
* **Walk-forward** (ADR-0034): in-sample 20 seancí vybere (D, H) s nejlepší
  vyváženou přesností 10denního výnosu od rozhodnutí, hodnotí se na dalších
  5 seancích; při shodě vyhrává baseline D = 1 σ, H = 10. Epizoda patří do
  okna podle dne začátku.
* **Éry**: kalibrace jen nad živou érou (od 2026-07-28, #640). Backfill éra
  (`--era backfill`, od 2024-07-28 = začátek barů) je informativní kontrola
  mechaniky bez vlivu na verdikt. Epizody začínající 10. 9. 2026 a později
  (ADR-0036, σ se ~100 seancí srovnává) se vykazují zvlášť.

Spuštění (z hostitele, PG publikované na 55432; URL se nikdy nevypisuje):
    uv run python scripts/measure_sentiment_episodes.py --db "$GEXLENS_HOST_DATABASE_URL" \\
        --symbols ES NQ --era both --out /tmp/tables.md

Výstup jsou jen tabulky; report `data/reports/sentiment-episodes-<datum>.md` = ručně
psané shrnutí s verdiktem (varianty + doporučení) následované těmito tabulkami.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import os
import statistics
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from gexlens_engine.compute.sentwaves import (  # noqa: E402
    MA_LONG,
    MA_SHORT,
    RISK_OFF,
    moving_average,
    position_state,
)
from gexlens_engine.compute.settle import settle_ts  # noqa: E402

DATA = Path(os.environ.get("GEXLENS_DATA_DIR", "data")) / "derived"
# Živá éra (#640): od tohoto dne se kalibruje; dřív je FF backfill s jiným mixem zdrojů
LIVE_ERA_START = dt.date(2026, 7, 28)
# Od tohoto dne platí váhy ADR-0036; σ(100) se ještě srovnává — vykazovat zvlášť
ADR36_START = dt.date(2026, 9, 10)
# Bary podkladu existují od tohoto dne (věčný archiv, ADR-0029)
BARS_START = dt.date(2024, 7, 28)

ROLLING_MAX_WINDOW = 20
GRID_D = (0.5, 1.0, 1.5, 2.0, 3.0)
GRID_H = (5, 10, 15)
FWD_HORIZONS = (5, 10, 20)
BASELINE = (1.0, 10)
# Walk-forward okna (ADR-0034 bod 3)
IN_SAMPLE = 20
OUT_SAMPLE = 5
WF_HORIZON = 10

VARIANT_DRAWDOWN = "A"
VARIANT_RISKOFF = "B"
# Pravidlo zahlazení (konec epizody = pokus):
#  peak   = close_z zpět NAD 20denním maximem z dne začátku (korekce plně smazána)
#  either = close_z nad hodnotou z dne začátku NEBO surový close nad MA10
#           (doslovné znění zadání „návrat nad úroveň začátku / nad MA10")
RECOVERY_PEAK = "peak"
RECOVERY_EITHER = "either"
RECOVERY_RULES = (RECOVERY_PEAK, RECOVERY_EITHER)
ATTEMPT = "attempt"
NEGATION = "negation"


@dataclass(frozen=True)
class DailyPoint:
    """Jeden řádek `sentiment_daily` obohacený o MA a stav (#563)."""

    date: dt.date
    close: float
    z: float | None
    sigma: float | None
    ma10: float | None
    state: str | None


@dataclass(frozen=True)
class Episode:
    symbol: str
    variant: str
    recovery: str
    threshold_d: float | None
    horizon_h: int
    start: dt.date
    ref_level: float  # 20denní maximum close_z v den začátku
    end: dt.date | None  # den rozhodnutí; None = cenzurováno (data končí dřív)
    label: str | None  # attempt / negation / None = cenzurováno
    depth_z: float
    length_days: int  # obchodní dny podkladu od začátku do rozhodnutí
    fwd_from_resolution: dict[int, float | None] = field(default_factory=dict)
    fwd_from_start: dict[int, float | None] = field(default_factory=dict)

    @property
    def post_adr36(self) -> bool:
        return self.start >= ADR36_START


@dataclass(frozen=True)
class ClassStats:
    n: int
    fwd_n: dict[int, int]
    fwd_mean: dict[int, float | None]
    fwd_median: dict[int, float | None]
    fwd_positive: dict[int, float | None]  # podíl kladných výnosů


@dataclass(frozen=True)
class GridCell:
    symbol: str
    variant: str
    threshold_d: float | None
    horizon_h: int
    attempts: ClassStats
    negations: ClassStats
    censored: int
    balanced_accuracy: dict[int, float | None]  # per horizont, kotva rozhodnutí
    mean_depth_attempt: float | None
    mean_depth_negation: float | None


# ── Vstupní řady ────────────────────────────────────────────────────────────


DailyRow = tuple[dt.date, float, float | None, float | None]


def enrich(rows: Sequence[DailyRow]) -> list[DailyPoint]:
    """Doplní MA5/MA10 a stav (#563) nad chronologickou řadou (date, close, close_z, σ).

    MA se počítají nad CELOU předanou řadou (včetně backfill éry), přesně jako
    `assess_state` v produkci — okno živé éry se vybírá až nad epizodami.
    """
    points: list[DailyPoint] = []
    closes: list[float] = []
    for day, close, z, sigma in rows:
        closes.append(close)
        ma5 = moving_average(closes, MA_SHORT)
        ma10 = moving_average(closes, MA_LONG)
        points.append(
            DailyPoint(
                date=day,
                close=close,
                z=z,
                sigma=sigma,
                ma10=ma10,
                state=position_state(close, ma5, ma10),
            )
        )
    return points


def rolling_max(values: Sequence[float | None], window: int) -> list[float | None]:
    """Klouzavé maximum posledních `window` hodnot včetně aktuální; None při mezeře."""
    out: list[float | None] = []
    for index, value in enumerate(values):
        chunk = values[max(0, index - window + 1) : index + 1]
        if value is None or any(v is None for v in chunk):
            out.append(None)
        else:
            out.append(max(v for v in chunk if v is not None))
    return out


# ── Detekce epizod ──────────────────────────────────────────────────────────


def episode_starts(
    points: Sequence[DailyPoint], variant: str, threshold_d: float | None
) -> list[tuple[int, float]]:
    """Indexy začátků epizod + referenční úroveň (20denní max close_z).

    Varianta A: drawdown ≥ D σ pod klouzavým maximem; další start až po odjištění
    (drawdown < D). Varianta B: den vstupu do RiskOff (předchozí den jiný stav).
    """
    maxima = rolling_max([p.z for p in points], ROLLING_MAX_WINDOW)
    starts: list[tuple[int, float]] = []
    if variant == VARIANT_DRAWDOWN:
        if threshold_d is None:
            raise ValueError("varianta A vyžaduje práh D")
        armed = True
        for index, point in enumerate(points):
            peak = maxima[index]
            if point.z is None or peak is None:
                continue
            drawdown = peak - point.z
            if drawdown < threshold_d:
                armed = True
                continue
            if armed:
                starts.append((index, peak))
                armed = False
        return starts
    if variant == VARIANT_RISKOFF:
        for index, point in enumerate(points):
            peak = maxima[index]
            if peak is None or point.state != RISK_OFF:
                continue
            if index > 0 and points[index - 1].state == RISK_OFF:
                continue
            starts.append((index, peak))
        return starts
    raise ValueError(f"neznámá varianta {variant!r}")


def is_recovered(point: DailyPoint, *, recovery: str, ref_level: float, start_z: float) -> bool:
    if recovery == RECOVERY_PEAK:
        return point.z is not None and point.z > ref_level
    if recovery == RECOVERY_EITHER:
        return (point.z is not None and point.z > start_z) or (
            point.ma10 is not None and point.close > point.ma10
        )
    raise ValueError(f"neznámé pravidlo zahlazení {recovery!r}")


def resolve_episode(
    points: Sequence[DailyPoint],
    start_index: int,
    ref_level: float,
    horizon_h: int,
    trading_days: Sequence[dt.date],
    *,
    recovery: str = RECOVERY_PEAK,
) -> tuple[dt.date | None, str | None, float, int]:
    """(den rozhodnutí, třída, depth_z, délka v obchodních dnech) pro jeden start.

    Zahlazení podle `recovery` (viz RECOVERY_RULES), hodnoceno od dne PO začátku.
    Horizont H se počítá v obchodních dnech podkladu (řada sentimentu má
    i víkendy). Bez dat do horizontu → cenzurováno.
    """
    start = points[start_index]
    start_z = start.z if start.z is not None else ref_level
    depth = max(0.0, ref_level - start_z)
    days_after_start = [d for d in trading_days if d > start.date]
    deadline = days_after_start[horizon_h - 1] if len(days_after_start) >= horizon_h else None
    for point in points[start_index + 1 :]:
        if deadline is not None and point.date > deadline:
            break
        if point.z is not None:
            depth = max(depth, ref_level - point.z)
        if is_recovered(point, recovery=recovery, ref_level=ref_level, start_z=start_z):
            length = sum(1 for d in days_after_start if d <= point.date)
            return point.date, ATTEMPT, depth, length
    if deadline is None or deadline > points[-1].date:
        return None, None, depth, len([d for d in days_after_start if d <= points[-1].date])
    return deadline, NEGATION, depth, horizon_h


def forward_returns(
    closes: dict[dt.date, float], trading_days: Sequence[dt.date], anchor: dt.date
) -> dict[int, float | None]:
    """Výnos podkladu (%) za 5/10/20 obchodních dní od posledního close ≤ kotva."""
    base_days = [d for d in trading_days if d <= anchor]
    if not base_days:
        return {h: None for h in FWD_HORIZONS}
    base_day = base_days[-1]
    base_index = trading_days.index(base_day)
    out: dict[int, float | None] = {}
    for horizon in FWD_HORIZONS:
        target = base_index + horizon
        if target >= len(trading_days):
            out[horizon] = None
            continue
        out[horizon] = (closes[trading_days[target]] / closes[base_day] - 1.0) * 100.0
    return out


def detect_episodes(
    symbol: str,
    points: Sequence[DailyPoint],
    closes: dict[dt.date, float],
    *,
    variant: str,
    threshold_d: float | None,
    horizon_h: int,
    era_start: dt.date,
    era_end: dt.date | None = None,
    recovery: str = RECOVERY_PEAK,
) -> list[Episode]:
    """Epizody dané varianty a parametrů začínající v éře [era_start, era_end].

    Epizody se NEpřekrývají: další smí začít až po dni rozhodnutí předchozí
    (stav „epizoda" ve fázi 2 může být jen jeden); cenzurovaná epizoda uzavírá
    řadu. Starty před érou se také rozhodují, aby blokovaly překryv na hranici.
    """
    trading_days = sorted(closes)
    episodes: list[Episode] = []
    last_end: dt.date | None = None
    for start_index, ref_level in episode_starts(points, variant, threshold_d):
        start = points[start_index]
        if era_end is not None and start.date > era_end:
            break
        if last_end is not None and start.date <= last_end:
            continue
        end, label, depth, length = resolve_episode(
            points, start_index, ref_level, horizon_h, trading_days, recovery=recovery
        )
        last_end = dt.date.max if end is None else end
        if start.date < era_start:
            continue
        episodes.append(
            Episode(
                symbol=symbol,
                variant=variant,
                recovery=recovery,
                threshold_d=threshold_d,
                horizon_h=horizon_h,
                start=start.date,
                ref_level=ref_level,
                end=end,
                label=label,
                depth_z=depth,
                length_days=length,
                fwd_from_resolution=(
                    forward_returns(closes, trading_days, end)
                    if end is not None
                    else {h: None for h in FWD_HORIZONS}
                ),
                fwd_from_start=forward_returns(closes, trading_days, start.date),
            )
        )
    return episodes


# ── Statistiky ──────────────────────────────────────────────────────────────


def class_stats(episodes: Sequence[Episode], *, anchor: str) -> ClassStats:
    fwd_n: dict[int, int] = {}
    fwd_mean: dict[int, float | None] = {}
    fwd_median: dict[int, float | None] = {}
    fwd_positive: dict[int, float | None] = {}
    for horizon in FWD_HORIZONS:
        values: list[float] = []
        for episode in episodes:
            fwd = episode.fwd_from_resolution if anchor == "resolution" else episode.fwd_from_start
            value = fwd[horizon]
            if value is not None:
                values.append(value)
        fwd_n[horizon] = len(values)
        fwd_mean[horizon] = statistics.fmean(values) if values else None
        fwd_median[horizon] = statistics.median(values) if values else None
        fwd_positive[horizon] = (sum(1 for v in values if v > 0) / len(values)) if values else None
    return ClassStats(
        n=len(episodes),
        fwd_n=fwd_n,
        fwd_mean=fwd_mean,
        fwd_median=fwd_median,
        fwd_positive=fwd_positive,
    )


def balanced_accuracy(episodes: Sequence[Episode], horizon: int) -> float | None:
    """½·(P[výnos>0 | pokus] + P[výnos≤0 | negace]) od dne rozhodnutí; None bez obou tříd."""
    attempts = [
        e.fwd_from_resolution[horizon]
        for e in episodes
        if e.label == ATTEMPT and e.fwd_from_resolution[horizon] is not None
    ]
    negations = [
        e.fwd_from_resolution[horizon]
        for e in episodes
        if e.label == NEGATION and e.fwd_from_resolution[horizon] is not None
    ]
    if not attempts or not negations:
        return None
    tpr = sum(1 for v in attempts if v is not None and v > 0) / len(attempts)
    tnr = sum(1 for v in negations if v is not None and v <= 0) / len(negations)
    return (tpr + tnr) / 2


def summarize(
    symbol: str,
    variant: str,
    threshold_d: float | None,
    horizon_h: int,
    episodes: Sequence[Episode],
    *,
    anchor: str = "resolution",
) -> GridCell:
    attempts = [e for e in episodes if e.label == ATTEMPT]
    negations = [e for e in episodes if e.label == NEGATION]
    censored = sum(1 for e in episodes if e.label is None)
    return GridCell(
        symbol=symbol,
        variant=variant,
        threshold_d=threshold_d,
        horizon_h=horizon_h,
        attempts=class_stats(attempts, anchor=anchor),
        negations=class_stats(negations, anchor=anchor),
        censored=censored,
        balanced_accuracy={h: balanced_accuracy(episodes, h) for h in FWD_HORIZONS},
        mean_depth_attempt=statistics.fmean([e.depth_z for e in attempts]) if attempts else None,
        mean_depth_negation=statistics.fmean([e.depth_z for e in negations]) if negations else None,
    )


# ── Walk-forward (ADR-0034) ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Fold:
    """Okna jsou půlotevřená [od, do) podle DATA: epizoda začínající o víkendu
    mezi dvěma seancemi patří do okna, které ten víkend obsahuje."""

    in_sample: tuple[dt.date, dt.date]
    out_sample: tuple[dt.date, dt.date]
    chosen: tuple[float, int]
    in_sample_metric: float | None
    oos_episodes: list[Episode]


def in_window(episode: Episode, window: tuple[dt.date, dt.date]) -> bool:
    return window[0] <= episode.start < window[1]


def grid_candidates() -> list[tuple[float, int]]:
    return [(d, h) for d in GRID_D for h in GRID_H]


def choose_candidate(
    by_candidate: dict[tuple[float, int], list[Episode]], window: tuple[dt.date, dt.date]
) -> tuple[tuple[float, int], float | None]:
    """Kandidát s nejlepší vyváženou přesností nad epizodami začínajícími v okně.

    Shoda (včetně „nikdo nemá obě třídy") → baseline (ADR-0034 bod 3).
    """
    best = BASELINE
    best_metric = balanced_accuracy(
        [e for e in by_candidate[BASELINE] if in_window(e, window)], WF_HORIZON
    )
    for candidate in grid_candidates():
        if candidate == BASELINE:
            continue
        metric = balanced_accuracy(
            [e for e in by_candidate[candidate] if in_window(e, window)], WF_HORIZON
        )
        if metric is None:
            continue
        if best_metric is None or metric > best_metric:
            best, best_metric = candidate, metric
    return best, best_metric


def walk_forward(
    by_candidate: dict[tuple[float, int], list[Episode]], trading_days: Sequence[dt.date]
) -> list[Fold]:
    """Folds IS 20 / OOS 5 seancí nad obchodními dny éry; epizoda patří do okna dnem začátku.

    Konec OOS okna = den po jeho poslední seanci (půlotevřeně); poslední fold
    smí být kratší jen tehdy, když má aspoň jednu OOS seanci — jinak by IS okno
    nemělo co hodnotit.
    """
    folds: list[Fold] = []
    start = 0
    while start + IN_SAMPLE < len(trading_days):
        is_window = (trading_days[start], trading_days[start + IN_SAMPLE])
        oos_last = min(start + IN_SAMPLE + OUT_SAMPLE, len(trading_days)) - 1
        oos_end = (
            trading_days[oos_last + 1]
            if oos_last + 1 < len(trading_days)
            else trading_days[oos_last] + dt.timedelta(days=1)
        )
        oos_window = (trading_days[start + IN_SAMPLE], oos_end)
        chosen, metric = choose_candidate(by_candidate, is_window)
        oos = [e for e in by_candidate[chosen] if in_window(e, oos_window)]
        folds.append(Fold(is_window, oos_window, chosen, metric, oos))
        start += OUT_SAMPLE
    return folds


# ── Načtení dat ─────────────────────────────────────────────────────────────


def load_daily(db_url: str, symbol: str) -> list[DailyRow]:
    from sqlalchemy import create_engine, text

    engine = create_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "select date, close, close_z, sigma from sentiment_daily "
                "where symbol = :symbol order by date"
            ),
            {"symbol": symbol},
        ).fetchall()
    return [
        (
            row.date,
            float(row.close),
            float(row.close_z) if row.close_z is not None else None,
            float(row.sigma) if row.sigma is not None else None,
        )
        for row in rows
    ]


def load_settle_closes(symbol: str) -> dict[dt.date, float]:
    """Close podkladu v settle (16:00 ET) per obchodní den z 1min barů (věčný archiv)."""
    import pandas as pd

    closes: dict[dt.date, float] = {}
    for path in sorted((DATA / symbol / "bars").glob("*.parquet")):
        day = dt.date.fromisoformat(path.stem)
        frame = pd.read_parquet(path, columns=["ts_min", "close"])
        if frame.empty:
            continue
        stamps = pd.to_datetime(frame.ts_min, utc=True)
        at_settle = frame[stamps <= pd.Timestamp(settle_ts(day))]
        if at_settle.empty:
            continue
        closes[day] = float(at_settle.close.iloc[-1])
    return closes


# ── Report ──────────────────────────────────────────────────────────────────


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f} %"


def _ratio(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _fwd_cell(stats: ClassStats, horizon: int) -> str:
    if stats.fwd_n[horizon] == 0:
        return "—"
    return f"{_pct(stats.fwd_mean[horizon])} (n={stats.fwd_n[horizon]})"


def _d_label(cell: GridCell) -> str:
    return "—" if cell.threshold_d is None else f"{cell.threshold_d:g}"


def render_grid(cells: Sequence[GridCell], *, title: str) -> list[str]:
    lines = [
        f"### {title}",
        "",
        "| Var | D σ | H | pokusů | negací | cenz. | Ø depth_z pokus / negace | "
        "BA 5d | BA 10d | BA 20d | pokus 5d | pokus 10d | pokus 20d | "
        "negace 5d | negace 10d | negace 20d |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cell in cells:
        lines.append(
            f"| {cell.variant} | {_d_label(cell)} | {cell.horizon_h} | {cell.attempts.n} | "
            f"{cell.negations.n} | {cell.censored} | "
            f"{_ratio(cell.mean_depth_attempt)} / {_ratio(cell.mean_depth_negation)} | "
            + " | ".join(_ratio(cell.balanced_accuracy[h]) for h in FWD_HORIZONS)
            + " | "
            + " | ".join(_fwd_cell(cell.attempts, h) for h in FWD_HORIZONS)
            + " | "
            + " | ".join(_fwd_cell(cell.negations, h) for h in FWD_HORIZONS)
            + " |"
        )
    lines.append("")
    return lines


def render_episodes(episodes: Sequence[Episode], *, title: str) -> list[str]:
    lines = [
        f"### {title}",
        "",
        "| Var | D σ | H | start | ref_level σ | rozhodnutí | třída | depth_z | délka | "
        "od rozhodnutí 5/10/20d | od startu 5/10/20d |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for e in sorted(episodes, key=lambda x: (x.variant, x.threshold_d or 0, x.horizon_h, x.start)):
        lines.append(
            f"| {e.variant} | {'—' if e.threshold_d is None else f'{e.threshold_d:g}'} | "
            f"{e.horizon_h} | {e.start} | {e.ref_level:+.2f} | {e.end or '—'} | "
            f"{e.label or 'cenzurováno'} | {e.depth_z:.2f} | {e.length_days} | "
            + " / ".join(_pct(e.fwd_from_resolution[h]) for h in FWD_HORIZONS)
            + " | "
            + " / ".join(_pct(e.fwd_from_start[h]) for h in FWD_HORIZONS)
            + " |"
        )
    lines.append("")
    return lines


def render_folds(folds: Sequence[Fold]) -> list[str]:
    lines = [
        "| In-sample | Out-of-sample | Zvolen (D, H) | IS BA 10d | OOS epizod | "
        "OOS pokus/negace/cenz. | OOS Ø 10d pokus | OOS Ø 10d negace |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for fold in folds:
        attempts = [e for e in fold.oos_episodes if e.label == ATTEMPT]
        negations = [e for e in fold.oos_episodes if e.label == NEGATION]
        censored = sum(1 for e in fold.oos_episodes if e.label is None)
        stat_a = class_stats(attempts, anchor="resolution")
        stat_n = class_stats(negations, anchor="resolution")
        lines.append(
            f"| {fold.in_sample[0]} → {fold.in_sample[1] - dt.timedelta(days=1)} | "
            f"{fold.out_sample[0]} → {fold.out_sample[1] - dt.timedelta(days=1)} | "
            f"({fold.chosen[0]:g}, {fold.chosen[1]}) | {_ratio(fold.in_sample_metric)} | "
            f"{len(fold.oos_episodes)} | {len(attempts)}/{len(negations)}/{censored} | "
            f"{_fwd_cell(stat_a, WF_HORIZON)} | {_fwd_cell(stat_n, WF_HORIZON)} |"
        )
    if folds:
        shares = {c: 0 for c in grid_candidates()}
        for fold in folds:
            shares[fold.chosen] += 1
        chosen = ", ".join(
            f"({d:g}, {h}) {n / len(folds):.0%}" for (d, h), n in shares.items() if n
        )
        pooled = [e for fold in folds for e in fold.oos_episodes]
        pooled_a = class_stats([e for e in pooled if e.label == ATTEMPT], anchor="resolution")
        pooled_n = class_stats([e for e in pooled if e.label == NEGATION], anchor="resolution")
        lines += [
            "",
            f"Podíl foldů: {chosen}",
            "",
            f"OOS slepenec voleb: pokusů {pooled_a.n}, negací {pooled_n.n}, "
            f"BA {WF_HORIZON}d {_ratio(balanced_accuracy(pooled, WF_HORIZON))}, "
            f"Ø {WF_HORIZON}d pokus {_fwd_cell(pooled_a, WF_HORIZON)} · "
            f"negace {_fwd_cell(pooled_n, WF_HORIZON)}",
        ]
    lines.append("")
    return lines


@dataclass(frozen=True)
class RuleResult:
    """Výsledky jednoho pravidla zahlazení nad celým gridem."""

    recovery: str
    cells: list[GridCell]
    cells_from_start: list[GridCell]
    episodes: list[Episode]
    folds: list[Fold]


@dataclass(frozen=True)
class SymbolResult:
    symbol: str
    era: str
    era_start: dt.date
    era_end: dt.date
    sentiment_days: int
    trading_days: int
    # σ(100) na začátku, uprostřed a na konci éry — dokládá, jak moc se měřítko hýbe
    sigma_path: list[tuple[dt.date, float | None, float | None]]
    rules: list[RuleResult]


def sigma_path(
    points: Sequence[DailyPoint], era_start: dt.date, era_end: dt.date
) -> list[tuple[dt.date, float | None, float | None]]:
    """(den, σ, close) na začátku, v polovině a na konci éry + den před ADR-0036."""
    in_era = [p for p in points if era_start <= p.date <= era_end]
    if not in_era:
        return []
    picks = [in_era[0], in_era[len(in_era) // 2], in_era[-1]]
    before_adr36 = [p for p in in_era if p.date < ADR36_START]
    if before_adr36 and before_adr36[-1] not in picks:
        picks.insert(2, before_adr36[-1])
    return [(p.date, p.sigma, p.close) for p in picks]


def measure_symbol(
    symbol: str,
    daily: Sequence[DailyRow],
    closes: dict[dt.date, float],
    *,
    era: str,
) -> SymbolResult:
    points = enrich(daily)
    if era == "live":
        era_start, era_end = LIVE_ERA_START, points[-1].date
    elif era == "backfill":
        era_start, era_end = BARS_START, LIVE_ERA_START - dt.timedelta(days=1)
    else:
        raise ValueError(f"neznámá éra {era!r}")
    era_days = [d for d in sorted(closes) if era_start <= d <= era_end]
    rules: list[RuleResult] = []
    for recovery in RECOVERY_RULES:
        all_episodes: list[Episode] = []
        cells: list[GridCell] = []
        cells_start: list[GridCell] = []
        by_candidate: dict[tuple[float, int], list[Episode]] = {}
        for variant in (VARIANT_DRAWDOWN, VARIANT_RISKOFF):
            d_values: Iterable[float | None] = GRID_D if variant == VARIANT_DRAWDOWN else (None,)
            for threshold_d, horizon_h in itertools.product(d_values, GRID_H):
                episodes = detect_episodes(
                    symbol,
                    points,
                    closes,
                    variant=variant,
                    threshold_d=threshold_d,
                    horizon_h=horizon_h,
                    era_start=era_start,
                    era_end=era_end,
                    recovery=recovery,
                )
                all_episodes.extend(episodes)
                # Hlavní tabulka bez epizod po ADR-0036 (σ zkreslená) — ty jdou zvlášť
                main = [e for e in episodes if not e.post_adr36]
                cells.append(summarize(symbol, variant, threshold_d, horizon_h, main))
                cells_start.append(
                    summarize(symbol, variant, threshold_d, horizon_h, main, anchor="start")
                )
                if threshold_d is not None:
                    by_candidate[(threshold_d, horizon_h)] = main
        rules.append(
            RuleResult(
                recovery=recovery,
                cells=cells,
                cells_from_start=cells_start,
                episodes=all_episodes,
                folds=walk_forward(by_candidate, era_days),
            )
        )
    return SymbolResult(
        symbol=symbol,
        era=era,
        era_start=era_start,
        era_end=era_end,
        sentiment_days=sum(1 for p in points if era_start <= p.date <= era_end),
        trading_days=len(era_days),
        sigma_path=sigma_path(points, era_start, era_end),
        rules=rules,
    )


def render_markdown(results: Sequence[SymbolResult], *, today: dt.date) -> str:
    lines = [
        f"# Korekční epizody SentIndexu — {today} · #565 fáze 1 · první měření, ne kalibrace",
        "",
        f"Grid D ∈ {{{', '.join(f'{d:g}' for d in GRID_D)}}} σ × "
        f"H ∈ {{{', '.join(map(str, GRID_H))}}}; "
        f"varianta A = drawdown pod 20denním maximem close_z, B = vstup do RiskOff (#563). "
        f"Zahlazení `peak` = close_z zpět nad 20denním maximem z dne začátku; "
        f"`either` = close_z nad hodnotou z dne začátku NEBO close nad MA10 (doslovné zadání). "
        f"Výnosy podkladu v % (settle 16:00 ET), kotva = den rozhodnutí (pokus: den zahlazení, "
        f"negace: start + H). BA = vyvážená přesnost mapování pokus→růst, negace→pokles; "
        f"< 0,5 = kontrariánské. Walk-forward IS {IN_SAMPLE} / OOS {OUT_SAMPLE} seancí, "
        f"metrika BA {WF_HORIZON}d, baseline (D {BASELINE[0]:g}, H {BASELINE[1]}). "
        f"Cenzurované epizody (horizont za koncem dat) se nevyhodnocují.",
        "",
    ]
    for result in results:
        era_label = "živá éra" if result.era == "live" else "backfill éra (informativní kontrola)"
        lines += [
            f"## {result.symbol} — {era_label} {result.era_start} → {result.era_end} · "
            f"{result.sentiment_days} dní řady · {result.trading_days} obchodních dní podkladu",
            "",
            "σ(100 seancí) v éře: "
            + " · ".join(
                f"{day}: σ {_ratio(sigma)} (close {close:+.2f})"
                for day, sigma, close in result.sigma_path
            ),
            "",
        ]
        for rule in result.rules:
            lines += render_grid(
                rule.cells,
                title=f"Zahlazení `{rule.recovery}` — grid, kotva den rozhodnutí "
                "(epizody před 10. 9. 2026)",
            )
            if rule.recovery == RECOVERY_PEAK:
                lines += render_grid(
                    rule.cells_from_start,
                    title=f"Zahlazení `{rule.recovery}` — grid, kotva den začátku "
                    "(jen popisné, třída ještě není známá)",
                )
            lines += [f"### Zahlazení `{rule.recovery}` — walk-forward (ADR-0034)", ""]
            lines += (
                render_folds(rule.folds)
                if rule.folds
                else ["(méně obchodních dní než IS + OOS)", ""]
            )
            if result.era != "live":
                continue
            baseline_eps = [
                e
                for e in rule.episodes
                if e.variant == VARIANT_DRAWDOWN
                and e.threshold_d == BASELINE[0]
                and e.horizon_h == BASELINE[1]
            ]
            riskoff_eps = [
                e
                for e in rule.episodes
                if e.variant == VARIANT_RISKOFF and e.horizon_h == BASELINE[1]
            ]
            lines += render_episodes(
                baseline_eps + riskoff_eps,
                title=f"Zahlazení `{rule.recovery}` — epizody baseline A "
                f"(D {BASELINE[0]:g}, H {BASELINE[1]}) a B (H {BASELINE[1]}), "
                "včetně epizod po 10. 9. 2026 (σ zkreslená, ADR-0036)",
            )
            post = [e for e in rule.episodes if e.post_adr36]
            lines += [
                f"Epizod začínajících 10. 9. 2026 a později napříč gridem: {len(post)} "
                f"(vyloučeny z gridu i walk-forwardu).",
                "",
            ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("GEXLENS_HOST_DATABASE_URL")
        or os.environ.get("GEXLENS_DATABASE_URL", ""),
        help="SQLAlchemy URL prod DB (jen čtení); hodnota se nikdy nevypisuje",
    )
    parser.add_argument("--symbols", nargs="+", default=["ES", "NQ"])
    parser.add_argument("--era", choices=["live", "backfill", "both"], default="live")
    parser.add_argument("--out", help="cesta k markdown reportu (jinak stdout)")
    args = parser.parse_args()
    if not args.db:
        parser.error("--db nebo GEXLENS_HOST_DATABASE_URL / GEXLENS_DATABASE_URL")

    eras = ["live", "backfill"] if args.era == "both" else [args.era]
    results: list[SymbolResult] = []
    for symbol in args.symbols:
        daily = load_daily(args.db, symbol)
        closes = load_settle_closes(symbol)
        if not daily or not closes:
            raise SystemExit(f"{symbol}: chybí sentiment_daily nebo bary podkladu")
        for era in eras:
            results.append(measure_symbol(symbol, daily, closes, era=era))
    report = render_markdown(results, today=dt.datetime.now(dt.UTC).date())
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"report: {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
