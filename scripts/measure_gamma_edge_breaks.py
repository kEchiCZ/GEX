"""Měření průrazů hranice gamma masy po #616 (#601 fáze 1, opakování nad plným řetězem).

Nic nedetekuje, nic nezapisuje — jen odpovídá na tři otázky z #601:

1. **Je vnější hrana masy po #616 vůbec určitelná** (leží uvnitř mřížky) při prazích
   10–50 % maxima, a to jak na uložené řadě `gexprofile` (IBKR obálka ±200 b), tak na
   profilu dopočítaném z plného řetězu (snapshoty tastytrade, ADR-0027)?
2. **Co následuje po průrazu** hrany: pohyb po směru v +5/+15/+30 min, MFE/MAE, podíl
   pokračování (Wilsonova dolní mez, verdikt jen nad `MIN_SAMPLE`), návrat dovnitř masy
   do 15/30 min. RTH a celá seance zvlášť, období před/po 25. 8. 2026 zvlášť.
3. **Překryv s T4 `gamma_momentum`** (průraz flipu): tentýž den a do 30 minut.

Kandidáti hrany (obě definice z enginu, žádná nová):

* ``core``  — `compute.gexfield.gamma_edges(profile, share)`: krajní bod, kde |NetGEX|
  ještě drží `share` × maximum profilu (produkční 0,85 = jádro; 0,10–0,50 = okraj masy).
* ``band``  — `compute.bandregime.band_zone(profile, price)`: kontura All 40 % z #575
  nad VÁŽENÝM profilem ($/1 %), souvislá tlumící zóna kolem ceny. Liší se od ``core``
  třemi věcmi: váha P²/100, jen kladná část profilu, souvislost od ceny (flip zónu utne).

Zdroje profilu:

* ``stored`` — uložená řada `derived/{sym}/{exp}/gexprofile` (co počítá produkce; front
  expirace = nejbližší expirace s profilem v té minutě). Po #616 ji stále píše jen IBKR
  runtime pro 0DTE/1DTE, takže mřížka zůstává ±200 b.
* ``chain``  — profil dopočítaný TOUTÉŽ formulí (`gamma_profile`, parita se ověřuje na
  první minutě každého souboru) z `snapshots/{sym}/{exp}` nejbližší expirace, jejíž řetěz
  je ten den široký (tasty pokrývá 2DTE+). Varianta ``chain±200`` bere z téhož řetězu
  jen striky do ±200 b od close — ukazuje, co plný řetěz na hraně změnil.

Metodika průrazu je stejná jako ve fázi 1 (`measure_gamma_edges.py`): první minuta, kdy
close přejde ven; dokud se cena nevrátí dovnitř, další průraz téže strany se nepočítá.
Hranice NA KRAJI mřížky se zahazuje (výjezd z mřížky není opuštění masy).

Spuštění (z kořene repa, data v `data/`):

    uv run python scripts/measure_gamma_edge_breaks.py --symbols ES NQ
    uv run python scripts/measure_gamma_edge_breaks.py --data D:/GEX/data --t4-csv t4.csv

CSV pro `--t4-csv` (jen SELECT): ``docker exec gex-postgres-1 psql -U gexlens -d gexlens
-Atc "copy (select symbol, expiry, direction, created_ts, entry, status, outcome_r from
setups where template='gamma_momentum' order by created_ts) to stdout with csv header"``.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import math
import os
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from gexlens_engine.compute.bandregime import BAND_ALL_SHARE, band_zone
from gexlens_engine.compute.gexfield import (
    GAMMA_EDGE_SHARE,
    TAU_FLOOR_S,
    GexProfile,
    ProfileContract,
    gamma_edges,
    gamma_profile,
)
from gexlens_engine.compute.settle import settle_ts
from gexlens_engine.compute.setupstats import wilson_lower_bound

# Okna následného pohybu a návratu (minuty)
HORIZONS = (5, 15, 30)
RETURN_WINDOWS = (15, 30)
# Prahy hrany: 0,85 = produkční jádro (#600), 0,10–0,50 = okraj masy z původní teze
SHARES = (0.10, 0.25, 0.40, 0.50, GAMMA_EDGE_SHARE)
#: Pod tímhle počtem událostí se verdikt nevydává. Repo drží 10 (týdenní sebekontrola
#: setupů, `setupstats`) a 60 (percentily, `volregime`); pro podíl pokračování s Wilsonovou
#: mezí je 30 nejmenší n, při kterém LB 95 % vůbec může překročit 0,5 (potřeba ≥ 21/30).
MIN_SAMPLE = 30
#: Nasazení #616 — plný řetěz ve snapshotech
FULL_CHAIN_SINCE = dt.date(2026, 8, 25)
#: Poloviční šířka IBKR obálky (ADR-0002) pro variantu chain±200
IBKR_HALF_WIDTH = 200.0
#: Minimální rozpětí použitelných striků (b), aby se řetěz počítal jako „plný"
WIDE_SPAN_MIN = 600.0
#: Stáří kotace, nad kterým produkce kontrakt z výpočtu vyřazuje (`quote_max_age_s`)
QUOTE_MAX_AGE_S = 900.0
_YEAR_S = 365.0 * 24 * 3600
_SQRT_2PI = math.sqrt(2.0 * math.pi)
NY = ZoneInfo("America/New_York")
RTH_OPEN = dt.time(9, 30)
RTH_CLOSE = dt.time(16, 0)


@dataclass(frozen=True)
class Break:
    symbol: str
    day: dt.date
    ts: dt.datetime
    side: str  # "up" | "dn"
    rth: bool
    moves: dict[int, float]
    mfe: dict[int, float]
    mae: dict[int, float]
    returned: dict[int, bool]


@dataclass(frozen=True)
class EdgeSeries:
    """Hranice per minuta + statistika určitelnosti."""

    label: str
    edges: dict[dt.datetime, tuple[float, float]]
    total: int
    clipped: int
    undefined: int

    @property
    def clipped_share(self) -> float:
        return self.clipped / self.total if self.total else 0.0


# ── Načítání ────────────────────────────────────────────────────────────


def _utc(value: object) -> dt.datetime:
    stamp = pd.Timestamp(value)
    naive: dt.datetime = stamp.to_pydatetime()
    if naive.tzinfo is None:
        return naive.replace(tzinfo=dt.UTC)
    return naive.astimezone(dt.UTC)


def is_rth(ts: dt.datetime) -> bool:
    local = ts.astimezone(NY)
    return local.weekday() < 5 and RTH_OPEN <= local.time() < RTH_CLOSE


def load_bars(data: str, symbol: str) -> pd.DataFrame:
    files = sorted(glob.glob(f"{data}/derived/{symbol}/bars/*.parquet"))
    if not files:
        raise SystemExit(f"{symbol}: chybí bary v {data}/derived/{symbol}/bars")
    frame = pd.concat([pd.read_parquet(f) for f in files])
    frame = frame.sort_values("ts_min").drop_duplicates("ts_min", keep="last")
    frame["ts"] = [_utc(t) for t in frame.ts_min]
    frame["day"] = [t.date() for t in frame.ts]
    frame["rth"] = [is_rth(t) for t in frame.ts]
    return frame.reset_index(drop=True)


def _profile_from_row(row: object) -> GexProfile:
    ts = _utc(row.ts_min)  # type: ignore[attr-defined]
    return GexProfile(
        ts_min=ts,
        grid_start=float(row.grid_start),  # type: ignore[attr-defined]
        grid_step=float(row.grid_step),  # type: ignore[attr-defined]
        values=tuple(float(v) for v in row.values),  # type: ignore[attr-defined]
    )


def stored_front_profiles(data: str, symbol: str) -> dict[dt.datetime, GexProfile]:
    """Uložený profil FRONT expirace per minuta (nejbližší expirace, která ji má)."""
    result: dict[dt.datetime, tuple[str, GexProfile]] = {}
    for path in sorted(glob.glob(f"{data}/derived/{symbol}/2*/gexprofile/*.parquet")):
        expiry = os.path.basename(os.path.dirname(os.path.dirname(path)))
        frame = pd.read_parquet(path)
        for row in frame.itertuples():
            profile = _profile_from_row(row)
            current = result.get(profile.ts_min)
            if current is None or expiry < current[0]:
                result[profile.ts_min] = (expiry, profile)
    return {ts: profile for ts, (_, profile) in result.items()}


def _usable_snapshot(path: str) -> pd.DataFrame:
    frame = pd.read_parquet(path, columns=["ts_min", "strike", "right", "iv", "oi", "stale_age"])
    mask = (frame.oi > 0) & (frame.iv > 0) & (frame.stale_age <= QUOTE_MAX_AGE_S)
    frame = frame[mask].copy()
    frame["ts"] = [_utc(t) for t in frame.ts_min]
    return frame


def wide_expiry_for_day(data: str, symbol: str, day: dt.date) -> tuple[str, pd.DataFrame] | None:
    """Nejbližší expirace, jejíž použitelný řetěz je ten den široký (medián rozpětí)."""
    stamp = day.isoformat()
    candidates = sorted(
        os.path.basename(os.path.dirname(p))
        for p in glob.glob(f"{data}/snapshots/{symbol}/2*/{stamp}.parquet")
    )
    for expiry in candidates:
        if expiry < day.strftime("%Y%m%d"):
            continue
        frame = _usable_snapshot(f"{data}/snapshots/{symbol}/{expiry}/{stamp}.parquet")
        if frame.empty:
            continue
        span = frame.groupby("ts").strike.agg(lambda s: s.max() - s.min())
        if float(span.median()) >= WIDE_SPAN_MIN:
            return expiry, frame
    return None


def _profile_numpy(
    strikes: np.ndarray,
    signs: np.ndarray,
    ivs: np.ndarray,
    ois: np.ndarray,
    grid: np.ndarray,
    tau_years: float,
) -> np.ndarray:
    """Vektorizovaná `gamma_profile` (r = q = 0) — čistý Python by na plném řetězu trval hodiny."""
    sqrt_tau = math.sqrt(tau_years)
    spot = grid[:, None]
    d1 = (np.log(spot / strikes[None, :]) + 0.5 * ivs[None, :] ** 2 * tau_years) / (
        ivs[None, :] * sqrt_tau
    )
    gamma = np.exp(-0.5 * d1 * d1) / (_SQRT_2PI * spot * ivs[None, :] * sqrt_tau)
    net: np.ndarray = (gamma * (signs * ois)[None, :]).sum(axis=1)
    return net


def chain_profiles(
    frame: pd.DataFrame,
    expiry: str,
    closes: dict[dt.datetime, float],
    *,
    half_width: float | None,
) -> dict[dt.datetime, GexProfile]:
    """Profily z řetězu produkční formulí; `half_width` ořeže striky na ±b od close.

    Mřížka stejně jako v `runtime`: [min strike, max strike], krok = nejmenší rozestup
    striků / 2. Multiplikátor 1 — hrany jsou podílové, měřítko na nich nic nemění.
    """
    settle = settle_ts(dt.datetime.strptime(expiry, "%Y%m%d").date())
    result: dict[dt.datetime, GexProfile] = {}
    checked = False
    for ts, minute in frame.groupby("ts"):
        close = closes.get(ts)
        if close is None:
            continue
        if half_width is not None:
            minute = minute[(minute.strike - close).abs() <= half_width]
        strikes_sorted = np.sort(minute.strike.unique())
        if len(strikes_sorted) < 2:
            continue
        step = float(np.min(np.diff(strikes_sorted))) / 2.0
        if step <= 0:
            continue
        start, stop = float(strikes_sorted[0]), float(strikes_sorted[-1])
        count = max(1, int(round((stop - start) / step)) + 1)
        grid = start + step * np.arange(count)
        tau_years = max((settle - ts).total_seconds(), TAU_FLOOR_S) / _YEAR_S
        values = _profile_numpy(
            minute.strike.to_numpy(float),
            np.where(minute.right.to_numpy() == "C", 1.0, -1.0),
            minute.iv.to_numpy(float),
            minute.oi.to_numpy(float),
            grid,
            tau_years,
        )
        profile = GexProfile(
            ts_min=ts, grid_start=start, grid_step=step, values=tuple(float(v) for v in values)
        )
        if not checked:
            _assert_parity(minute, profile, settle)
            checked = True
        result[ts] = profile
    return result


def _assert_parity(minute: pd.DataFrame, profile: GexProfile, settle: dt.datetime) -> None:
    """Numpy profil musí být totožný s `gamma_profile` z enginu — jinak měříme něco jiného."""
    contracts = [
        ProfileContract(strike=float(r.strike), right=str(r.right), iv=float(r.iv), oi=float(r.oi))
        for r in minute.itertuples()
    ]
    reference = gamma_profile(
        contracts,
        ts_min=profile.ts_min,
        settle=settle,
        grid_start=profile.grid_start,
        grid_stop=profile.grid_start + (len(profile.values) - 1) * profile.grid_step,
        grid_step=profile.grid_step,
        multiplier=1.0,
    )
    scale = max(abs(v) for v in reference.values) or 1.0
    worst = max(abs(a - b) for a, b in zip(reference.values, profile.values, strict=True))
    if worst > 1e-9 * scale:
        raise RuntimeError(f"Parita profilu selhala v {profile.ts_min}: max odchylka {worst:g}")


# ── Hranice ─────────────────────────────────────────────────────────────


def core_edges(label: str, profiles: dict[dt.datetime, GexProfile], share: float) -> EdgeSeries:
    edges: dict[dt.datetime, tuple[float, float]] = {}
    total = clipped = undefined = 0
    for ts, profile in profiles.items():
        total += 1
        found = gamma_edges(profile, share=share)
        if found.up is None or found.dn is None:
            undefined += 1
            continue
        top = profile.grid_start + (len(profile.values) - 1) * profile.grid_step
        if abs(found.up - top) < 1e-9 or abs(found.dn - profile.grid_start) < 1e-9:
            clipped += 1
            continue
        edges[ts] = (found.dn, found.up)
    return EdgeSeries(label, edges, total, clipped, undefined)


def band_edges(
    label: str, profiles: dict[dt.datetime, GexProfile], closes: dict[dt.datetime, float]
) -> EdgeSeries:
    """Kontura All 40 % (#575): `band_zone` vrací None i na kraji mřížky → clipped."""
    edges: dict[dt.datetime, tuple[float, float]] = {}
    total = clipped = undefined = 0
    for ts, profile in profiles.items():
        close = closes.get(ts)
        if close is None:
            continue
        total += 1
        zone = band_zone(profile, close)
        if zone is None:
            clipped += 1
            continue
        edges[ts] = (zone.all_low, zone.all_high)
    return EdgeSeries(label, edges, total, clipped, undefined)


# ── Průrazy ─────────────────────────────────────────────────────────────


def find_breaks(symbol: str, series: EdgeSeries, bars: pd.DataFrame) -> list[Break]:
    found: list[Break] = []
    for day, group in bars.groupby("day"):
        group = group.reset_index(drop=True)
        stamps: list[dt.datetime] = group.ts.tolist()
        closes = group.close.astype(float).tolist()
        highs = group.high.astype(float).tolist()
        lows = group.low.astype(float).tolist()
        rth = group.rth.tolist()
        # Hranice se v mezerách drží poslední známá (jen uvnitř dne)
        edges: list[tuple[float, float] | None] = []
        last: tuple[float, float] | None = None
        for ts in stamps:
            last = series.edges.get(ts, last)
            edges.append(last)
        outside = {"up": False, "dn": False}
        for index, ts in enumerate(stamps):
            edge = edges[index]
            if edge is None or ts not in series.edges:
                # Bez hranice v této minutě se stav neresetuje ani neprorazí
                continue
            dn, up = edge
            close = closes[index]
            for side, beyond in (("up", close > up), ("dn", close < dn)):
                if not beyond:
                    outside[side] = False
                    continue
                if outside[side]:
                    continue
                outside[side] = True
                sign = 1.0 if side == "up" else -1.0
                moves: dict[int, float] = {}
                mfe: dict[int, float] = {}
                mae: dict[int, float] = {}
                for horizon in HORIZONS:
                    stop = min(index + horizon, len(closes) - 1)
                    moves[horizon] = sign * (closes[stop] - close)
                    window_high = max(highs[index : stop + 1])
                    window_low = min(lows[index : stop + 1])
                    mfe[horizon] = (window_high - close) if sign > 0 else (close - window_low)
                    mae[horizon] = (close - window_low) if sign > 0 else (window_high - close)
                returned: dict[int, bool] = {}
                for window in RETURN_WINDOWS:
                    back = False
                    for ahead in range(index + 1, min(index + window + 1, len(stamps))):
                        future = edges[ahead]
                        if future is None:
                            continue
                        f_dn, f_up = future
                        if (side == "up" and closes[ahead] <= f_up) or (
                            side == "dn" and closes[ahead] >= f_dn
                        ):
                            back = True
                            break
                    returned[window] = back
                found.append(
                    Break(
                        symbol=symbol,
                        day=day,
                        ts=ts,
                        side=side,
                        rth=bool(rth[index]),
                        moves=moves,
                        mfe=mfe,
                        mae=mae,
                        returned=returned,
                    )
                )
    return found


# ── Výstup ──────────────────────────────────────────────────────────────


def verdict(rows: Sequence[Break]) -> str:
    """Verdikt nad +15 min: směr (Wilson LB > 0,5) A velikost (MFE > MAE, návrat < 50 %).

    Samotný podíl pokračování nestačí: cena oscilující těsně za hranou dá 60 % „po směru"
    s mediánem +0,5 b a návratem do masy v 98 % případů — to je šum na hraně, ne průraz.
    """
    n = len(rows)
    if n < MIN_SAMPLE:
        return f"nerozhodnuto (n={n} < {MIN_SAMPLE})"
    wins = sum(1 for row in rows if row.moves[15] > 0)
    lower = wilson_lower_bound(wins, n)
    upper = 1.0 - wilson_lower_bound(n - wins, n)
    mfe = statistics.median(row.mfe[15] for row in rows)
    mae = statistics.median(row.mae[15] for row in rows)
    back = sum(1 for row in rows if row.returned[15]) / n
    if lower > 0.5:
        if mfe > mae and back < 0.5:
            return f"hrana existuje (LB {lower:.2f} > 0,5, MFE > MAE, návrat {back:.0%})"
        return (
            f"směr ano, průraz ne (LB {lower:.2f} > 0,5, ale MFE {mfe:.2f} vs. MAE {mae:.2f}, "
            f"návrat do 15 min {back:.0%})"
        )
    if upper < 0.5:
        return f"opak průrazu (UB {upper:.2f} < 0,5)"
    return f"hrana neexistuje — nerozlišitelné od mince (LB {lower:.2f}, UB {upper:.2f})"


def edge_distance(series: EdgeSeries, closes: dict[dt.datetime, float]) -> float | None:
    """Medián vzdálenosti bližší hrany od close (b) — jak daleko průraz vůbec je."""
    distances = [
        min(up - close, close - dn)
        for ts, (dn, up) in series.edges.items()
        if (close := closes.get(ts)) is not None
    ]
    return statistics.median(distances) if distances else None


def describe(rows: Sequence[Break], days: int) -> None:
    if not rows:
        print("    (žádný průraz)")
        return
    per_day = len(rows) / days if days else float("nan")
    print(f"    průrazů {len(rows)} za {days} dní ({per_day:.1f}/den)")
    for horizon in HORIZONS:
        moves = [row.moves[horizon] for row in rows]
        wins = sum(1 for m in moves if m > 0)
        print(
            f"    +{horizon:>2} min: medián {statistics.median(moves):+6.2f} b · "
            f"Ø {statistics.fmean(moves):+6.2f} b · po směru {wins / len(moves):4.0%} "
            f"(Wilson LB {wilson_lower_bound(wins, len(moves)):.2f}) · "
            f"MFE {statistics.median([r.mfe[horizon] for r in rows]):5.2f} · "
            f"MAE {statistics.median([r.mae[horizon] for r in rows]):5.2f}"
        )
    for window in RETURN_WINDOWS:
        back = sum(1 for row in rows if row.returned[window])
        print(f"    návrat dovnitř do {window} min: {back / len(rows):.0%}")
    false_breaks = sum(1 for row in rows if row.returned[15] and row.moves[15] <= 0)
    print(f"    falešných (fáze 1: návrat do 15 min a pohyb ≤ 0): {false_breaks / len(rows):.0%}")
    print(f"    verdikt (+15 min): {verdict(rows)}")


def report(
    series: EdgeSeries,
    rows: Sequence[Break],
    days_with_edge: int,
    closes: dict[dt.datetime, float],
) -> None:
    print(
        f"  [{series.label}] minut s profilem {series.total} · hranice na kraji mřížky "
        f"{series.clipped_share:.0%} · neurčitelná {series.undefined} · "
        f"použitelných minut {len(series.edges)}"
    )
    if not series.edges:
        return
    distance = edge_distance(series, closes)
    if distance is not None:
        print(f"   medián vzdálenosti bližší hrany od close: {distance:.1f} b")
    for session, label in ((None, "celá seance"), (True, "RTH")):
        subset = [r for r in rows if session is None or r.rth == session]
        print(
            f"   {label}: nahoru {sum(r.side == 'up' for r in subset)} / dolů "
            f"{sum(r.side == 'dn' for r in subset)}"
        )
        describe(subset, days_with_edge)


def per_day_table(rows: Sequence[Break]) -> None:
    counts: dict[dt.date, list[int]] = {}
    for row in rows:
        cell = counts.setdefault(row.day, [0, 0, 0, 0])
        cell[0 if row.side == "up" else 1] += 1
        if row.rth:
            cell[2 if row.side == "up" else 3] += 1
    print("    den        | seance ↑/↓ | RTH ↑/↓")
    for day in sorted(counts):
        up, dn, rup, rdn = counts[day]
        print(f"    {day} | {up:3d}/{dn:<3d}    | {rup:3d}/{rdn:<3d}")


@dataclass(frozen=True)
class T4Event:
    symbol: str
    ts: dt.datetime
    direction: str


def load_t4(path: str | None) -> list[T4Event]:
    if not path:
        return []
    with open(path, encoding="utf-8", newline="") as handle:
        return [
            T4Event(row["symbol"], _utc(row["created_ts"]), row["direction"])
            for row in csv.DictReader(handle)
        ]


def t4_overlap(label: str, events: Sequence[T4Event], rows: Sequence[Break]) -> None:
    if not events:
        return
    same_day = within_30 = 0
    for event in events:
        side = "up" if event.direction == "long" else "dn"
        day = event.ts.date()
        candidates = [r for r in rows if r.symbol == event.symbol and r.side == side]
        if any(r.day == day for r in candidates):
            same_day += 1
        if any(abs((r.ts - event.ts).total_seconds()) <= 30 * 60 for r in candidates):
            within_30 += 1
    print(
        f"  T4 překryv [{label}]: {len(events)} setupů · tentýž den a strana "
        f"{same_day} ({same_day / len(events):.0%}) · do 30 min {within_30} "
        f"({within_30 / len(events):.0%})"
    )


def _period(rows: Iterable[Break], since: dt.date, before: bool) -> list[Break]:
    return [r for r in rows if (r.day < since) == before]


# ── Hlavní běh ──────────────────────────────────────────────────────────


def run_symbol(data: str, symbol: str, t4: Sequence[T4Event], shares: Sequence[float]) -> None:
    bars = load_bars(data, symbol)
    closes = dict(zip(bars.ts, bars.close.astype(float), strict=True))
    print(f"\n════════ {symbol} ════════")

    # A) uložená řada gexprofile — front expirace, před/po #616
    stored = stored_front_profiles(data, symbol)
    print(f"A) uložený gexprofile (IBKR obálka): {len(stored)} minut")
    stored_series: list[EdgeSeries] = [
        core_edges(f"stored core {share:.2f}", stored, share) for share in shares
    ]
    stored_series.append(band_edges(f"stored band All {BAND_ALL_SHARE:.2f}", stored, closes))
    stored_breaks: dict[str, list[Break]] = {}
    for series in stored_series:
        rows = find_breaks(symbol, series, bars)
        stored_breaks[series.label] = rows
        report(series, rows, len({ts.date() for ts in series.edges}), closes)
        periods = ((True, f"před {FULL_CHAIN_SINCE}"), (False, f"od {FULL_CHAIN_SINCE}"))
        for before, label in periods:
            subset_edges = {
                ts: e for ts, e in series.edges.items() if (ts.date() < FULL_CHAIN_SINCE) == before
            }
            minutes = [ts for ts in stored if (ts.date() < FULL_CHAIN_SINCE) == before]
            if not minutes:
                continue
            clipped = sum(1 for ts in minutes if ts not in series.edges)
            period_series = EdgeSeries(
                f"{series.label} · {label}", subset_edges, len(minutes), clipped, 0
            )
            day_count = len({ts.date() for ts in subset_edges})
            report(period_series, _period(rows, FULL_CHAIN_SINCE, before), day_count, closes)

    # B) plný řetěz ze snapshotů (jen od #616)
    print(f"\nB) plný řetěz ze snapshotů (od {FULL_CHAIN_SINCE}, nejbližší široká expirace)")
    chain_days = sorted({d for d in bars.day.unique() if d >= FULL_CHAIN_SINCE})
    full: dict[dt.datetime, GexProfile] = {}
    narrow: dict[dt.datetime, GexProfile] = {}
    for day in chain_days:
        chosen = wide_expiry_for_day(data, symbol, day)
        if chosen is None:
            print(f"  {day}: žádná expirace se širokým řetězem")
            continue
        expiry, frame = chosen
        full.update(chain_profiles(frame, expiry, closes, half_width=None))
        narrow.update(chain_profiles(frame, expiry, closes, half_width=IBKR_HALF_WIDTH))
        print(
            f"  {day}: expirace {expiry}, {frame.ts.nunique()} minut, "
            f"{frame.strike.nunique()} striků"
        )
    chain_breaks: dict[str, list[Break]] = {}
    for label, profiles in (("chain", full), (f"chain±{IBKR_HALF_WIDTH:.0f}", narrow)):
        for share in shares:
            series = core_edges(f"{label} core {share:.2f}", profiles, share)
            rows = find_breaks(symbol, series, bars)
            chain_breaks[series.label] = rows
            report(series, rows, len({ts.date() for ts in series.edges}), closes)
        series = band_edges(f"{label} band All {BAND_ALL_SHARE:.2f}", profiles, closes)
        rows = find_breaks(symbol, series, bars)
        chain_breaks[series.label] = rows
        report(series, rows, len({ts.date() for ts in series.edges}), closes)

    # C) per den, D) překryv s T4
    print("\nC) průrazy per den")
    for label in (f"stored core {GAMMA_EDGE_SHARE:.2f}", "chain core 0.10", "chain core 0.40"):
        rows = stored_breaks.get(label) or chain_breaks.get(label) or []
        if rows:
            print(f"  [{label}]")
            per_day_table(rows)
    print("\nD) překryv s T4 gamma_momentum")
    mine = [e for e in t4 if e.symbol == symbol]
    for label, rows in list(stored_breaks.items()) + list(chain_breaks.items()):
        if rows:
            t4_overlap(label, mine, rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--symbols", nargs="+", default=["ES", "NQ"])
    parser.add_argument("--data", default="data", help="kořen dat (derived/ a snapshots/)")
    parser.add_argument("--shares", nargs="+", type=float, default=list(SHARES))
    parser.add_argument("--t4-csv", default=None, help="CSV setupů gamma_momentum z PG")
    args = parser.parse_args()
    t4 = load_t4(args.t4_csv)
    print(f"MIN_SAMPLE={MIN_SAMPLE}, horizonty {HORIZONS} min, návrat {RETURN_WINDOWS} min")
    for symbol in args.symbols:
        run_symbol(args.data, symbol, t4, args.shares)


if __name__ == "__main__":
    main()
