"""Kde je signál v reakcích ES/NQ na ohlášené USD releasy (#1296, fáze 2 — měření).

Výzkumný skript nad datasetem `scripts/build_release_reactions.py` (release × symbol).
Nic nezapisuje do PG ani do `data/`: PG jen čte (spojení s `default_transaction_read_only`,
historie překvapení řad od 8/2023), minutové bary jen čte (pohyb 24 h před releasem).
Výstupy (report, CSV, cache) jdou do `--out-dir`.

Co se měří (ES a NQ zvlášť, každý horizont 5/15/60 min a 1/2/3/5/10 seancí):

1. **Podmíněný směr** — P(pokles | překvapení nad odhadem) a P(pokles | pod odhadem),
   Wilsonův 95% interval, Fisherův exaktní test 2×2. „Nad odhadem" = ekonomické znaménko
   (`surprise_econ_sign`: +1 silnější ekonomika / teplejší inflace / jestřábí Fed; polarita
   řady už je v něm — vyšší nezaměstnanost je „pod odhadem"). Krátké horizonty měří
   surový výnos, vícedenní abnormální (proti baseline ±60 seancí). FOMC: FF forecast se
   trefí 17× z 18, podmínka je proto rozhodnutí (snížení vs. beze změny).
2. **Velikost** — medián a p75 |výchylky| (max. pohyb v okně) a |abnormálního pohybu|
   per řada × horizont × volatilitní tercil; percentil výchylky proti stejné minutě dne.
3. **Nepodmíněný sklon před releasem** — P(růst) proti 50 %, pohyb 60 min a 24 h před
   releasem, volatilitní režim (point-in-time), minulé překvapení řady (i perzistence
   překvapení samotného). GEX režim má jen 34 releasů — netestuje se.
4. **Vícedenní** — abnormální pohyb 1–10 seancí podle znaménka překvapení (Mann-Whitney,
   exaktní rozdělení do n = 60), citlivost na překryv (bez FOMC, bez tier-1 releasu,
   striktně bez High releasu).
5. **Walk-forward** — odhad na prvních 2/3 období (IS), ověření na poslední 1/3 (OOS):
   jednostranný test ve směru IS odhadu. Vícedenní IS okna končící za řezem se vyřadí.
6. **Vícenásobné testování** — Benjamini-Hochberg přes celou rodinu směrových testů
   (1 + 3 + 4); testy velikosti (volatility) mají vlastní rodinu, aby jejich jisté
   efekty nezměkčovaly práh směrovým testům.

Kritéria stanovená předem (před pohledem na výsledky):

* **prošlo**: BH q ≤ 0,10 v rodině počítané **jen z IS p-hodnot** (`q_is`), OOS ve směru IS
  s jednostranným p ≤ 0,10;
* **kandidát**: q_is ≤ 0,10, OOS ve stejném směru, ale nepotvrzený (p > 0,10);
* **nestabilní**: q_is ≤ 0,10, ale OOS opačný směr;
* **bez efektu**: q_is > 0,10; **nedostatek dat**: méně než 5 releasů v některé buňce.

Metodická korekce 26. 9. 2026 (před fází 3, ne ladění kritérií): první verze rozhodovala
podle q z **celého** vzorku a vyžadovala „směr IS = směr celku“ — celý vzorek ale obsahuje
OOS, takže OOS se použil dvakrát (k objevu i k ověření). Objev teď stojí jen na IS
(Benjamini-Hochberg přes IS p-hodnoty rodiny), OOS slouží jen k jednostrannému ověření ve
směru IS. q z celého vzorku zůstává v reportu jen informativně. Výjimka: stabilita pořadí
velikosti řad je sama srovnáním IS → OOS (nemá vlastní IS fázi) a rozhoduje u ní q rodiny.

Spuštění (z hostitele; URL ani heslo se nevypisují):
    python scripts/measure_release_predictability.py --out-dir <scratchpad>/epic1296 \\
        --data-dir D:/…/GEX/data --env-file D:/…/GEX/.env
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

SYMBOLS = ("ES", "NQ")
SHORT_H = ("5m", "15m", "60m")
DAILY_H = ("1d", "2d", "3d", "5d", "10d")
HORIZONS = SHORT_H + DAILY_H
#: Horizonty pro sklon před releasem (dál už rozhoduje překvapení a další události)
PRE_H = ("5m", "15m", "60m", "1d")
#: Minimum releasů v každé buňce, aby se test vůbec počítal
MIN_CELL = 5
IS_FRACTION = 2 / 3
Q_PASS = 0.10
OOS_ALPHA = 0.10
#: Do tolika pozorování celkem exaktní rozdělení Mann-Whitney U, nad tím normální aproximace
MW_EXACT_MAX = 60
#: Hranice velkého překvapení pro rozpad podle síly (|surprise_z_pit|)
BIG_Z = 0.5
#: Inflační releasy do tolika kalendářních dní od začátku epizody tvoří jednu epizodu (post-hoc)
EPISODE_DAYS = 7
#: Tier-1 události pro citlivost vícedenních oken na překryv
TIER1_SERIES = (
    "Core CPI m/m",
    "Core PPI m/m",
    "Core PCE Price Index m/m",
    "Non-Farm Employment Change",
    "Federal Funds Rate",
)
KEY_SERIES = (
    "Core CPI m/m",
    "CPI m/m",
    "Core PPI m/m",
    "PPI m/m",
    "Core PCE Price Index m/m",
    "Non-Farm Employment Change",
    "Unemployment Rate",
    "Average Hourly Earnings m/m",
    "Unemployment Claims",
    "ADP Non-Farm Employment Change",
    "JOLTS Job Openings",
    "Retail Sales m/m",
    "ISM Manufacturing PMI",
    "ISM Services PMI",
    "Prelim UoM Consumer Sentiment",
    "CB Consumer Confidence",
    "Federal Funds Rate",
)
GROUPS = ("inflation", "labor", "growth", "fed", "sentiment", "housing", "energy")
GROUP_CZ = {
    "inflation": "inflace",
    "labor": "trh práce",
    "growth": "růst",
    "fed": "Fed",
    "sentiment": "sentiment",
    "housing": "bydlení",
    "energy": "energie",
    "all": "všechny releasy",
}
FOMC = "Federal Funds Rate"
PREDICTOR_CZ = {
    "surprise_sign": "znaménko překvapení",
    "fomc_cut_vs_hold": "FOMC snížení vs. beze změny",
    "drift": "nepodmíněný sklon P(růst)",
    "pre_60m": "pohyb 60 min před",
    "pre_24h": "pohyb 24 h před",
    "vol_regime": "vol. režim high vs. low (PIT)",
    "prev_surprise": "minulé překvapení řady → směr",
    "persist_prev1": "perzistence překvapení (minulé)",
    "persist_prev3": "perzistence překvapení (většina 3 minulých)",
    "surprise_abn_mw": "abnormální pohyb podle překvapení (MW)",
    "fomc_abn_mw": "FOMC abnormální pohyb snížení vs. beze změny (MW)",
    "exc_vs_baseline": "výchylka nad baseline (exc_pct > 50)",
    "size_rank_stability": "stabilita pořadí velikosti řad IS→OOS",
}


# ── Statistika ─────────────────────────────────────────────────────


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilsonův interval spolehlivosti pro podíl (oboustranný 95 %)."""
    if n <= 0:
        return (math.nan, math.nan)
    phat = k / n
    denom = 1.0 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _log_comb(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def fisher(k_a: int, n_a: int, k_b: int, n_b: int, alternative: str = "two-sided") -> float:
    """Fisherův exaktní test 2×2; `greater` = podíl v A větší než v B."""
    total = k_a + k_b
    size = n_a + n_b
    lo, hi = max(0, total - n_b), min(total, n_a)
    xs = np.arange(lo, hi + 1)
    logp = np.array(
        [
            _log_comb(total, x) + _log_comb(size - total, n_a - x) - _log_comb(size, n_a)
            for x in range(lo, hi + 1)
        ]
    )
    pmf = np.exp(logp)
    if alternative == "greater":
        return float(min(1.0, pmf[xs >= k_a].sum()))
    if alternative == "less":
        return float(min(1.0, pmf[xs <= k_a].sum()))
    observed = pmf[xs == k_a][0]
    return float(min(1.0, pmf[pmf <= observed * (1 + 1e-7)].sum()))


def binom_test(k: int, n: int, alternative: str = "two-sided") -> float:
    """Exaktní binomický test proti p = 0,5."""
    xs = np.arange(n + 1)
    pmf = np.exp(np.array([_log_comb(n, x) for x in range(n + 1)]) - n * math.log(2))
    if alternative == "greater":
        return float(min(1.0, pmf[xs >= k].sum()))
    if alternative == "less":
        return float(min(1.0, pmf[xs <= k].sum()))
    return float(min(1.0, pmf[pmf <= pmf[k] * (1 + 1e-7)].sum()))


@cache
def mw_null(m: int, n: int) -> np.ndarray:
    """Exaktní rozdělení U (počet párů a > b) pod H0 pro velikosti m, n (bez shod)."""
    previous = [np.ones(1) for _ in range(n + 1)]  # i = 0 → U = 0
    for i in range(1, m + 1):
        row = [np.ones(1)]  # j = 0 → U = 0
        for j in range(1, n + 1):
            dist = np.zeros(i * j + 1)
            a = previous[j]  # největší prvek je z A → přispěje j páry
            dist[j : j + len(a)] += i / (i + j) * a
            b = row[j - 1]  # největší prvek je z B → nepřispěje nic
            dist[: len(b)] += j / (i + j) * b
            row.append(dist)
        previous = row
    result: np.ndarray = previous[n]
    return result


def _norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def mann_whitney(a: np.ndarray, b: np.ndarray, alternative: str = "two-sided") -> float:
    """Mann-Whitney U; `greater` = hodnoty A mají sklon být větší než B."""
    m, n = len(a), len(b)
    ranks = pd.Series(np.concatenate([a, b])).rank(method="average").to_numpy()
    u = float(ranks[:m].sum() - m * (m + 1) / 2)
    if m + n <= MW_EXACT_MAX:
        dist = mw_null(m, n)
        p_le = float(dist[: math.floor(u + 1e-9) + 1].sum())
        p_ge = float(dist[math.ceil(u - 1e-9) :].sum())
    else:
        size = m + n
        _, counts = np.unique(np.concatenate([a, b]), return_counts=True)
        tie = float(((counts**3) - counts).sum()) / (size * (size - 1))
        sigma = math.sqrt(m * n / 12 * ((size + 1) - tie))
        mu = m * n / 2
        p_ge = _norm_sf((u - mu - 0.5) / sigma)
        p_le = 1 - _norm_sf((u - mu + 0.5) / sigma)
    if alternative == "greater":
        return min(1.0, p_ge)
    if alternative == "less":
        return min(1.0, p_le)
    return min(1.0, 2 * min(p_le, p_ge))


def spearman_perm(
    x: np.ndarray, y: np.ndarray, rng: np.random.Generator, draws: int = 20000
) -> tuple[float, float]:
    """Spearmanovo ρ a oboustranné permutační p."""
    rx = pd.Series(x).rank().to_numpy()
    ry = pd.Series(y).rank().to_numpy()
    rx = (rx - rx.mean()) / rx.std()
    ry = (ry - ry.mean()) / ry.std()
    rho = float((rx * ry).mean())
    perms = np.argsort(rng.random((draws, len(ry))), axis=1)
    null = (rx[None, :] * ry[perms]).mean(axis=1)
    p = (1 + int((np.abs(null) >= abs(rho) - 1e-12).sum())) / (draws + 1)
    return rho, p


def benjamini_hochberg(p: np.ndarray) -> np.ndarray:
    m = len(p)
    if m == 0:
        return p
    order = np.argsort(p)
    ranked = p[order] * m / np.arange(1, m + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.minimum(q, 1.0)
    return out


# ── Příprava dat ───────────────────────────────────────────────────


def move_col(h: str) -> str:
    """Krátké horizonty surový výnos, vícedenní abnormální (proti baseline)."""
    return f"ret_{h}_bp" if h in SHORT_H else f"ret_abn_{h}_bp"


def as_bool(values: pd.Series) -> np.ndarray:
    """Objektový sloupec True/False/None → bool (None = False)."""
    flags = values.map(lambda v: bool(v) if isinstance(v, bool | np.bool_) else False)
    result: np.ndarray = flags.to_numpy(dtype=bool)
    return result


def compute_pre24h(data_dir: Path, frame: pd.DataFrame) -> pd.DataFrame:
    """Pohyb 24 h před releasem (close minuty před releasem proti close ≤ T−24 h), back-adjusted."""
    import build_release_reactions as brr

    raw: dict[str, pd.DataFrame] = {}
    for symbol in SYMBOLS:
        loaded = brr.load_bars(data_dir, symbol)
        cleaned, _ = brr.drop_mixed_contract_runs(loaded, symbol)
        raw[symbol] = cleaned
    switches = brr.detect_roll_switches(raw)
    t0 = min(bars["ts"].iloc[0] for bars in raw.values()).floor("D")
    t1 = max(bars["ts"].iloc[-1] for bars in raw.values()) + pd.Timedelta(days=8)
    rows: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        grid = brr.build_grid(raw[symbol], t0, t1, switches, symbol)
        subset = frame[frame["symbol"] == symbol]
        for event_id, cluster_ts in zip(subset["event_id"], subset["cluster_ts"], strict=True):
            m = grid.index(cluster_ts.to_pydatetime())
            value = math.nan
            if m >= 1 and not math.isnan(grid.close[m - 1]):
                ref = brr.last_valid_before(grid, m - 1440)
                if ref is not None:
                    value = (grid.close[m - 1] - grid.close[ref]) / grid.close[ref] * 1e4
            rows.append({"event_id": event_id, "symbol": symbol, "pre_24h_bp": value})
        del grid
    return pd.DataFrame(rows)


def load_surprise_history(env_file: Path) -> pd.DataFrame:
    """Znaménka překvapení všech releasů řad z DB (od 8/2023, libovolný impact), jen čtení."""
    import build_release_reactions as brr

    engine = brr.read_only_engine(env_file)
    try:
        events = brr.load_events(engine)
    finally:
        engine.dispose()
    rows = []
    for event in events:
        series = event.title[4:]
        if series not in brr.SERIES or event.actual is None or event.forecast is None:
            continue
        polarity = brr.SERIES[series][1]
        rows.append(
            {
                "event_id": event.id,
                "series": series,
                "ts": event.ts,
                "econ_sign": brr.sign(event.actual - event.forecast) * polarity,
            }
        )
    history = pd.DataFrame(rows).sort_values(["series", "ts", "event_id"])
    history = history.drop_duplicates(["series", "ts"], keep="last").reset_index(drop=True)
    grouped = history.groupby("series")["econ_sign"]
    history["prev1"] = grouped.shift(1)
    history["prev3_sum"] = grouped.transform(lambda s: s.shift(1).rolling(3).sum())
    history["prev3"] = np.sign(history["prev3_sum"])
    return history


def tier1_overlap(frame: pd.DataFrame) -> pd.DataFrame:
    """Počet tier-1 releasů (CPI, PPI, PCE, NFP, FOMC) v (release, settle konce okna]."""
    from gexlens_engine.compute.settle import settle_ts

    naive = frame["cluster_ts"].dt.tz_convert("UTC").dt.tz_localize(None).astype("datetime64[us]")
    minutes = np.sort(naive[frame["series"].isin(TIER1_SERIES)].unique())
    starts = naive.to_numpy()
    out = pd.DataFrame(index=frame.index)
    cache: dict[dt.date, np.datetime64] = {}
    for h in DAILY_H:
        values = np.full(len(frame), np.nan)
        for position, end_day in enumerate(frame[f"end_session_{h}"]):
            if not isinstance(end_day, dt.date):
                continue
            if end_day not in cache:
                cache[end_day] = (
                    pd.Timestamp(settle_ts(end_day))
                    .tz_convert("UTC")
                    .tz_localize(None)
                    .to_datetime64()
                    .astype("datetime64[us]")
                )
            lo = np.searchsorted(minutes, starts[position], side="right")
            hi = np.searchsorted(minutes, cache[end_day], side="right")
            values[position] = hi - lo
        out[f"overlap_t1_{h}"] = values
    return out


def prepare(args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    frame = pd.read_parquet(args.dataset)
    frame["cluster_ts"] = pd.to_datetime(frame["cluster_ts"], utc=True)
    frame["ts_event"] = pd.to_datetime(frame["ts_event"], utc=True)

    pre_path = args.out_dir / "cache_pre24h.parquet"
    if pre_path.exists() and not args.refresh:
        pre = pd.read_parquet(pre_path)
    else:
        print("Počítám pohyb 24 h před releasem z barů…")
        pre = compute_pre24h(args.data_dir, frame)
        pre.to_parquet(pre_path, index=False)
    frame = frame.merge(pre, on=["event_id", "symbol"], how="left", validate="one_to_one")

    hist_path = args.out_dir / "cache_surprise_history.parquet"
    if hist_path.exists() and not args.refresh:
        history = pd.read_parquet(hist_path)
    else:
        print("Čtu historii překvapení z PG (read-only)…")
        history = load_surprise_history(args.env_file)
        history.to_parquet(hist_path, index=False)
    history["ts"] = pd.to_datetime(history["ts"], utc=True)
    frame = frame.merge(
        history[["event_id", "prev1", "prev3"]], on="event_id", how="left", validate="many_to_one"
    )
    frame = pd.concat([frame, tier1_overlap(frame)], axis=1)

    start, end = frame["ts_event"].min(), frame["ts_event"].max()
    cut = start + (end - start) * IS_FRACTION
    frame["is_oos"] = frame["ts_event"] >= cut
    cut_day = pd.Timestamp(cut.date())
    for h in DAILY_H:
        ends = pd.to_datetime(frame[f"end_session_{h}"], errors="coerce")
        frame[f"is_ok_{h}"] = (~frame["is_oos"]).to_numpy() & (ends < cut_day).to_numpy()
    for h in SHORT_H:
        frame[f"is_ok_{h}"] = ~frame["is_oos"]
    frame["fomc_change"] = np.where(
        frame["series"] == FOMC, np.sign(frame["actual"] - frame["previous"]), np.nan
    )
    return frame, history, cut


def unit_frames(frame: pd.DataFrame, symbol: str) -> dict[tuple[str, str], pd.DataFrame]:
    """Jednotky analýzy: skupina (headline na shluk a skupinu), řada, vše (headline shluku)."""
    sub = frame[frame["symbol"] == symbol]
    units: dict[tuple[str, str], pd.DataFrame] = {("all", "all"): sub[sub["primary_in_cluster"]]}
    for group in GROUPS:
        units[("group", group)] = sub[sub["primary_in_group"] & (sub["group"] == group)]
    for series in sorted(sub["series"].unique()):
        units[("series", series)] = sub[sub["series"] == series]
    return units


# ── Testy ──────────────────────────────────────────────────────────


def _dir(value: float) -> int:
    if math.isnan(value) or value == 0:
        return 0
    return 1 if value > 0 else -1


def test_2x2(
    cond_a: np.ndarray,
    cond_b: np.ndarray,
    outcome: np.ndarray,
    is_mask: np.ndarray,
    oos_mask: np.ndarray,
) -> dict[str, Any] | None:
    """P(outcome | A) vs P(outcome | B): Fisher, Wilson, IS/OOS, pravidlo směru a jeho hit rate."""

    def counts(mask: np.ndarray) -> tuple[int, int, int, int]:
        a, b = cond_a & mask, cond_b & mask
        return int(a.sum()), int((outcome & a).sum()), int(b.sum()), int((outcome & b).sum())

    everything = np.ones(len(outcome), dtype=bool)
    n_a, k_a, n_b, k_b = counts(everything)
    if n_a < MIN_CELL or n_b < MIN_CELL:
        return None
    result: dict[str, Any] = {
        "n_a": n_a,
        "k_a": k_a,
        "n_b": n_b,
        "k_b": k_b,
        "p_a": k_a / n_a,
        "p_b": k_b / n_b,
        "effect": k_a / n_a - k_b / n_b,
        "p": fisher(k_a, n_a, k_b, n_b),
    }
    result["ci_a"], result["ci_b"] = wilson(k_a, n_a), wilson(k_b, n_b)
    direction = _dir(result["effect"])
    hits = k_a + (n_b - k_b) if direction >= 0 else (n_a - k_a) + k_b
    result["hit"] = hits / (n_a + n_b)
    result["hit_ci"] = wilson(hits, n_a + n_b)

    i_a, ik_a, i_b, ik_b = counts(is_mask)
    result["is_n"] = i_a + i_b
    result["is_effect"] = ik_a / i_a - ik_b / i_b if i_a and i_b else math.nan
    result["is_p"] = fisher(ik_a, i_a, ik_b, i_b) if i_a and i_b else math.nan
    o_a, ok_a, o_b, ok_b = counts(oos_mask)
    result["oos_n"] = o_a + o_b
    result["oos_n_a"], result["oos_n_b"] = o_a, o_b
    result["oos_effect"] = ok_a / o_a - ok_b / o_b if o_a and o_b else math.nan
    is_dir = _dir(result["is_effect"])
    result["oos_p"] = math.nan
    result["oos_hit"] = math.nan
    result["oos_hit_ci"] = (math.nan, math.nan)
    if is_dir != 0 and o_a and o_b:
        result["oos_p"] = fisher(ok_a, o_a, ok_b, o_b, "greater" if is_dir > 0 else "less")
        oos_hits = ok_a + (o_b - ok_b) if is_dir > 0 else (o_a - ok_a) + ok_b
        result["oos_hit"] = oos_hits / (o_a + o_b)
        result["oos_hit_ci"] = wilson(oos_hits, o_a + o_b)
    return result


def test_binomial(
    outcome: np.ndarray, is_mask: np.ndarray, oos_mask: np.ndarray
) -> dict[str, Any] | None:
    """P(outcome) proti 50 % (nepodmíněný sklon)."""
    n, k = len(outcome), int(outcome.sum())
    if n < 2 * MIN_CELL:
        return None
    result: dict[str, Any] = {
        "n_a": n,
        "k_a": k,
        "p_a": k / n,
        "ci_a": wilson(k, n),
        "effect": k / n - 0.5,
        "p": binom_test(k, n),
    }
    n_is, k_is = int(is_mask.sum()), int((outcome & is_mask).sum())
    n_oos, k_oos = int(oos_mask.sum()), int((outcome & oos_mask).sum())
    result["is_n"], result["oos_n"] = n_is, n_oos
    result["is_effect"] = k_is / n_is - 0.5 if n_is else math.nan
    result["is_p"] = binom_test(k_is, n_is) if n_is else math.nan
    result["oos_effect"] = k_oos / n_oos - 0.5 if n_oos else math.nan
    is_dir = _dir(result["is_effect"])
    result["oos_p"] = math.nan
    result["oos_hit"] = math.nan
    result["oos_hit_ci"] = (math.nan, math.nan)
    if is_dir != 0 and n_oos:
        result["oos_p"] = binom_test(k_oos, n_oos, "greater" if is_dir > 0 else "less")
        hits = k_oos if is_dir > 0 else n_oos - k_oos
        result["oos_hit"] = hits / n_oos
        result["oos_hit_ci"] = wilson(hits, n_oos)
    direction = _dir(result["effect"])
    hits_all = k if direction >= 0 else n - k
    result["hit"] = hits_all / n
    result["hit_ci"] = wilson(hits_all, n)
    return result


def test_mw(
    values: np.ndarray,
    cond_a: np.ndarray,
    cond_b: np.ndarray,
    is_mask: np.ndarray,
    oos_mask: np.ndarray,
) -> dict[str, Any] | None:
    """Abnormální pohyb A vs. B: medián rozdílu, Mann-Whitney, IS/OOS."""
    a, b = values[cond_a], values[cond_b]
    if len(a) < MIN_CELL or len(b) < MIN_CELL:
        return None
    result: dict[str, Any] = {
        "n_a": len(a),
        "n_b": len(b),
        "med_a": float(np.median(a)),
        "med_b": float(np.median(b)),
        "mean_a": float(a.mean()),
        "mean_b": float(b.mean()),
        "effect": float(np.median(a) - np.median(b)),
        "p": mann_whitney(a, b),
    }
    ia, ib = values[cond_a & is_mask], values[cond_b & is_mask]
    oa, ob = values[cond_a & oos_mask], values[cond_b & oos_mask]
    result["is_n"], result["oos_n"] = len(ia) + len(ib), len(oa) + len(ob)
    result["oos_n_a"], result["oos_n_b"] = len(oa), len(ob)
    result["is_effect"] = float(np.median(ia) - np.median(ib)) if len(ia) and len(ib) else math.nan
    result["is_p"] = mann_whitney(ia, ib) if len(ia) and len(ib) else math.nan
    result["oos_effect"] = float(np.median(oa) - np.median(ob)) if len(oa) and len(ob) else math.nan
    is_dir = _dir(result["is_effect"])
    result["oos_p"] = math.nan
    if is_dir != 0 and len(oa) and len(ob):
        result["oos_p"] = mann_whitney(oa, ob, "greater" if is_dir > 0 else "less")
    return result


def record(
    rows: list[dict[str, Any]],
    base: dict[str, Any],
    result: dict[str, Any] | None,
) -> None:
    if result is None:
        rows.append({**base, "tested": False})
    else:
        rows.append({**base, "tested": True, **result})


def direction_tests(frame: pd.DataFrame, history: pd.DataFrame, cut: pd.Timestamp) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        for (kind, unit), sub in unit_frames(frame, symbol).items():
            econ = sub["surprise_econ_sign"].to_numpy(dtype=float)
            oos = sub["is_oos"].to_numpy()
            for h in HORIZONS:
                values = sub[move_col(h)].to_numpy(dtype=float)
                valid = ~np.isnan(values) & (values != 0)
                is_ok = sub[f"is_ok_{h}"].to_numpy() & valid
                oos_ok = oos & valid
                down = values < 0
                base = {"symbol": symbol, "unit_kind": kind, "unit": unit, "horizon": h}

                # 1) Podmíněný směr podle znaménka překvapení (ne pro „vše" — znaménko
                #    překvapení znamená v každé skupině něco jiného)
                if kind != "all" and unit not in ("fed", FOMC):
                    record(
                        rows,
                        {**base, "section": "S1", "predictor": "surprise_sign"},
                        test_2x2(valid & (econ > 0), valid & (econ < 0), down, is_ok, oos_ok),
                    )
                if kind == "series" and unit == FOMC:
                    change = sub["fomc_change"].to_numpy(dtype=float)
                    record(
                        rows,
                        {**base, "section": "S1", "predictor": "fomc_cut_vs_hold"},
                        test_2x2(valid & (change < 0), valid & (change == 0), down, is_ok, oos_ok),
                    )

                # 4) Vícedenní abnormální pohyb podle překvapení (Mann-Whitney)
                if h in DAILY_H and kind != "all" and unit != "fed":
                    if unit == FOMC:
                        change = sub["fomc_change"].to_numpy(dtype=float)
                        cond_a, cond_b, name = change < 0, change == 0, "fomc_abn_mw"
                    else:
                        cond_a, cond_b, name = econ > 0, econ < 0, "surprise_abn_mw"
                    finite = ~np.isnan(values)
                    record(
                        rows,
                        {**base, "section": "S4", "predictor": name},
                        test_mw(
                            np.nan_to_num(values),
                            finite & cond_a,
                            finite & cond_b,
                            sub[f"is_ok_{h}"].to_numpy(),
                            oos,
                        ),
                    )

                # 3) Sklon před releasem — vše, skupiny a klíčové řady
                if h not in PRE_H or (kind == "series" and unit not in KEY_SERIES):
                    continue
                up = values > 0
                record(
                    rows,
                    {**base, "section": "S3", "predictor": "drift"},
                    test_binomial(up[valid], is_ok[valid], oos_ok[valid]),
                )
                if kind == "series":
                    prev = sub["prev1"].to_numpy(dtype=float)
                    if unit != FOMC:
                        record(
                            rows,
                            {**base, "section": "S3", "predictor": "prev_surprise"},
                            test_2x2(valid & (prev > 0), valid & (prev < 0), down, is_ok, oos_ok),
                        )
                    continue
                for name, column in (("pre_60m", "pre_60m_bp"), ("pre_24h", "pre_24h_bp")):
                    pre = sub[column].to_numpy(dtype=float)
                    record(
                        rows,
                        {**base, "section": "S3", "predictor": name},
                        test_2x2(valid & (pre > 0), valid & (pre < 0), up, is_ok, oos_ok),
                    )
                regime = sub["vol_tercile_pit"].astype(object).to_numpy()
                record(
                    rows,
                    {**base, "section": "S3", "predictor": "vol_regime"},
                    test_2x2(
                        valid & (regime == "high"), valid & (regime == "low"), up, is_ok, oos_ok
                    ),
                )
                if kind == "group" and unit != "fed":
                    prev = sub["prev1"].to_numpy(dtype=float)
                    record(
                        rows,
                        {**base, "section": "S3", "predictor": "prev_surprise"},
                        test_2x2(valid & (prev > 0), valid & (prev < 0), down, is_ok, oos_ok),
                    )

    # 3b) Perzistence překvapení (bez symbolu a horizontu): předpoví minulé překvapení to příští?
    headline = headline_series(frame)
    history = history.copy()
    history["oos"] = history["ts"] >= cut
    units: list[tuple[str, str, pd.DataFrame]] = [
        ("series", series, part) for series, part in history.groupby("series")
    ]
    for group in GROUPS:
        members = [s for s, g in headline.items() if g == group]
        units.append(("group", group, history[history["series"].isin(members)]))
    for kind, unit, part in units:
        current = part["econ_sign"].to_numpy(dtype=float)
        valid = current != 0
        positive = current > 0
        oos = part["oos"].to_numpy()
        for name, column in (("persist_prev1", "prev1"), ("persist_prev3", "prev3")):
            prev = part[column].to_numpy(dtype=float)
            record(
                rows,
                {
                    "symbol": "—",
                    "unit_kind": kind,
                    "unit": unit,
                    "horizon": "—",
                    "section": "S3",
                    "predictor": name,
                },
                test_2x2(
                    valid & (prev > 0), valid & (prev < 0), positive, valid & ~oos, valid & oos
                ),
            )
    return pd.DataFrame(rows)


def headline_series(frame: pd.DataFrame) -> dict[str, str]:
    """Řady, které jsou headline skupiny aspoň v polovině svých releasů → skupina."""
    es = frame[frame["symbol"] == SYMBOLS[0]]
    share = es.groupby("series")["primary_in_group"].mean()
    groups = es.groupby("series")["group"].first()
    return {series: groups[series] for series, value in share.items() if value >= 0.5}


def overlap_sensitivity(frame: pd.DataFrame, tests: pd.DataFrame) -> pd.DataFrame:
    """Vícedenní testy (S1 i S4) znovu bez oken s FOMC / tier-1 / jakýmkoli High releasem."""
    specs: dict[str, Callable[[pd.DataFrame, str], np.ndarray]] = {
        "bez FOMC": lambda sub, h: ~as_bool(sub[f"overlap_fomc_{h}"]),
        "bez tier-1": lambda sub, h: sub[f"overlap_t1_{h}"].to_numpy(dtype=float) == 0,
        "striktně bez High": lambda sub, h: sub[f"overlap_n_{h}"].to_numpy(dtype=float) == 0,
    }
    selected = tests[
        tests["tested"]
        & tests["horizon"].isin(DAILY_H)
        & tests["predictor"].isin(
            ["surprise_sign", "surprise_abn_mw", "fomc_abn_mw", "fomc_cut_vs_hold"]
        )
    ]
    rows = []
    units = {symbol: unit_frames(frame, symbol) for symbol in SYMBOLS}
    for _, test in selected.iterrows():
        sub = units[test["symbol"]][(test["unit_kind"], test["unit"])]
        h = test["horizon"]
        values = sub[move_col(h)].to_numpy(dtype=float)
        if test["unit"] == FOMC:
            change = sub["fomc_change"].to_numpy(dtype=float)
            cond_a, cond_b = change < 0, change == 0
        else:
            econ = sub["surprise_econ_sign"].to_numpy(dtype=float)
            cond_a, cond_b = econ > 0, econ < 0
        for spec, keep_fn in specs.items():
            keep = keep_fn(sub, h) & ~np.isnan(values) & (values != 0)
            row = {
                "symbol": test["symbol"],
                "unit_kind": test["unit_kind"],
                "unit": test["unit"],
                "horizon": h,
                "predictor": test["predictor"],
                "spec": spec,
                "n_a": int((keep & cond_a).sum()),
                "n_b": int((keep & cond_b).sum()),
            }
            if row["n_a"] >= MIN_CELL and row["n_b"] >= MIN_CELL:
                a, b = values[keep & cond_a], values[keep & cond_b]
                if test["predictor"] in ("surprise_sign", "fomc_cut_vs_hold"):
                    k_a, k_b = int((a < 0).sum()), int((b < 0).sum())
                    row["effect"] = k_a / len(a) - k_b / len(b)
                    row["p"] = fisher(k_a, len(a), k_b, len(b))
                else:
                    row["effect"] = float(np.median(a) - np.median(b))
                    row["p"] = mann_whitney(a, b)
            rows.append(row)
    return pd.DataFrame(rows)


# ── Velikost (volatilita) ──────────────────────────────────────────


def size_tables(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Medián a p75 |výchylky| a |abnormálního pohybu| per jednotka × horizont × režim."""
    rows = []
    for symbol in SYMBOLS:
        for (kind, unit), sub in unit_frames(frame, symbol).items():
            for regime in ("vše", "low", "mid", "high"):
                part = sub if regime == "vše" else sub[sub["vol_tercile"] == regime]
                for h in HORIZONS:
                    size_col = f"exc_{h}_bp" if h in SHORT_H else f"ret_{h}_bp"
                    size = part[size_col].abs().dropna()
                    abn = part[f"ret_abn_{h}_bp"].abs().dropna()
                    row = {
                        "symbol": symbol,
                        "unit_kind": kind,
                        "unit": unit,
                        "regime": regime,
                        "horizon": h,
                        "n": len(size),
                        "size_med": size.median() if len(size) else math.nan,
                        "size_p75": size.quantile(0.75) if len(size) else math.nan,
                        "abn_med": abn.median() if len(abn) else math.nan,
                        "abn_p75": abn.quantile(0.75) if len(abn) else math.nan,
                    }
                    if h in SHORT_H:
                        pct = part[f"exc_pct_{h}"].dropna()
                        row["exc_pct_med"] = pct.median() if len(pct) else math.nan
                    rows.append(row)
    sizes = pd.DataFrame(rows)
    # IS vs OOS medián velikosti (stabilita „typické velikosti" řady)
    stab_rows = []
    for symbol in SYMBOLS:
        for (kind, unit), sub in unit_frames(frame, symbol).items():
            for h in HORIZONS:
                size_col = f"exc_{h}_bp" if h in SHORT_H else f"ret_abn_{h}_bp"
                ok = sub[f"is_ok_{h}"]
                is_part = sub.loc[ok, size_col].abs().dropna()
                oos_part = sub.loc[sub["is_oos"], size_col].abs().dropna()
                stab_rows.append(
                    {
                        "symbol": symbol,
                        "unit_kind": kind,
                        "unit": unit,
                        "horizon": h,
                        "is_n": len(is_part),
                        "is_med": is_part.median() if len(is_part) else math.nan,
                        "oos_n": len(oos_part),
                        "oos_med": oos_part.median() if len(oos_part) else math.nan,
                    }
                )
    return sizes, pd.DataFrame(stab_rows)


def volatility_tests(
    frame: pd.DataFrame, stability: pd.DataFrame, rng: np.random.Generator
) -> pd.DataFrame:
    """Vlastní rodina: výchylka po releasu větší než v téže minutě běžného dne? Drží pořadí řad?"""
    rows: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        for (kind, unit), sub in unit_frames(frame, symbol).items():
            if kind == "all":
                continue
            for h in SHORT_H:
                pct = sub[f"exc_pct_{h}"].to_numpy(dtype=float)
                valid = ~np.isnan(pct)
                above = pct > 50
                record(
                    rows,
                    {
                        "symbol": symbol,
                        "unit_kind": kind,
                        "unit": unit,
                        "horizon": h,
                        "section": "S2",
                        "predictor": "exc_vs_baseline",
                    },
                    test_binomial(
                        above[valid],
                        sub[f"is_ok_{h}"].to_numpy()[valid],
                        sub["is_oos"].to_numpy()[valid],
                    ),
                )
    for symbol in SYMBOLS:
        for h in HORIZONS:
            part = stability[
                (stability["symbol"] == symbol)
                & (stability["horizon"] == h)
                & (stability["unit_kind"] == "series")
                & (stability["is_n"] >= MIN_CELL)
                & (stability["oos_n"] >= MIN_CELL)
            ]
            if len(part) < 8:
                continue
            rho, p = spearman_perm(part["is_med"].to_numpy(), part["oos_med"].to_numpy(), rng)
            rows.append(
                {
                    "symbol": symbol,
                    "unit_kind": "series",
                    "unit": f"{len(part)} řad",
                    "horizon": h,
                    "section": "S2",
                    "predictor": "size_rank_stability",
                    "tested": True,
                    "n_a": len(part),
                    "effect": rho,
                    "p": p,
                    "is_n": len(part),
                    "oos_n": len(part),
                    "is_effect": rho,
                    "oos_effect": rho,
                    "oos_p": p,
                }
            )
    return pd.DataFrame(rows)


# ── Post-hoc (po pohledu na OOS — mimo rodinu a mimo verdikty) ─────


def posthoc_inflation(frame: pd.DataFrame) -> pd.DataFrame:
    """Jádro inflace (headline shluku = CPI/PPI/PCE, bez ISM Prices a deflátoru) × síla překvapení.

    Vzniklo PO pohledu na OOS (inflační skupina v OOS slábla, v OOS rozbíjely vzor řádky
    ISM Manufacturing Prices — trh tam reaguje na headline ISM PMI). OOS proto už není čistý;
    výsledek je hypotéza k ověření na nových datech (fáze 4), ne prošlý signál.
    """
    rows: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        sub = frame[
            (frame["symbol"] == symbol)
            & frame["primary_in_cluster"]
            & (frame["group"] == "inflation")
        ]
        econ = sub["surprise_econ_sign"].to_numpy(dtype=float)
        absz = sub["surprise_z_pit"].abs().to_numpy(dtype=float)
        oos = sub["is_oos"].to_numpy()
        for h in HORIZONS:
            values = sub[move_col(h)].to_numpy(dtype=float)
            valid = ~np.isnan(values) & (values != 0)
            is_ok = sub[f"is_ok_{h}"].to_numpy() & valid
            for label, zmask in (
                ("vše", np.ones(len(sub), dtype=bool)),
                ("|z| ≥ 0,5", absz >= BIG_Z),
                ("|z| < 0,5", absz < BIG_Z),
            ):
                base = {
                    "symbol": symbol,
                    "horizon": h,
                    "z_split": label,
                    "series": ", ".join(sorted(sub["series"].unique())),
                }
                keep = valid & zmask
                if h in SHORT_H or label == "vše":
                    result = test_2x2(
                        keep & (econ > 0), keep & (econ < 0), values < 0, is_ok & zmask, oos & keep
                    )
                    if result is not None:
                        rows.append({**base, "kind": "směr", **result})
                if h in DAILY_H and label == "vše":
                    finite = ~np.isnan(values)
                    result = test_mw(
                        np.nan_to_num(values),
                        finite & (econ > 0),
                        finite & (econ < 0),
                        sub[f"is_ok_{h}"].to_numpy(),
                        oos,
                    )
                    if result is not None:
                        rows.append({**base, "kind": "abn MW", **result})
        # Epizody: CPI, PPI a PCE do 7 dní od sebe se vícedenními okny překrývají a jejich
        # překvapení korelují → jedno pozorování na epizodu (znaménko součtu, okno od 1. releasu)
        ordered = sub.sort_values("ts_event")
        episode, start, labels = -1, None, []
        for ts in ordered["ts_event"]:
            if start is None or (ts - start).days > EPISODE_DAYS:
                episode, start = episode + 1, ts
            labels.append(episode)
        grouped = ordered.assign(episode=labels).groupby("episode")
        episodes = grouped.agg(
            ts_event=("ts_event", "first"),
            econ=("surprise_econ_sign", "sum"),
            **{f"abn_{h}": (move_col(h), "first") for h in DAILY_H},
            **{f"is_ok_{h}": (f"is_ok_{h}", "first") for h in DAILY_H},
        )
        ep_econ = np.sign(episodes["econ"].to_numpy(dtype=float))
        ep_oos = (
            episodes["ts_event"] >= ordered.loc[ordered["is_oos"], "ts_event"].min()
        ).to_numpy()
        for h in DAILY_H:
            values = episodes[f"abn_{h}"].to_numpy(dtype=float)
            finite = ~np.isnan(values)
            result = test_mw(
                np.nan_to_num(values),
                finite & (ep_econ > 0),
                finite & (ep_econ < 0),
                episodes[f"is_ok_{h}"].to_numpy(dtype=bool),
                ep_oos,
            )
            if result is not None:
                rows.append(
                    {
                        "symbol": symbol,
                        "horizon": h,
                        "z_split": f"epizody ≤ {EPISODE_DAYS} dní ({len(episodes)})",
                        "series": "epizody",
                        "kind": "abn MW epizody",
                        **result,
                    }
                )
    return pd.DataFrame(rows)


# ── Verdikty ───────────────────────────────────────────────────────


def verdicts(tests: pd.DataFrame) -> pd.DataFrame:
    tests = tests.copy()
    tested = tests["tested"].fillna(False).astype(bool) & tests["p"].notna()
    tests["q"] = np.nan
    tests.loc[tested, "q"] = benjamini_hochberg(tests.loc[tested, "p"].to_numpy(dtype=float))
    for section in tests["section"].unique():
        mask = tested & (tests["section"] == section)
        tests.loc[mask, "q_section"] = benjamini_hochberg(
            tests.loc[mask, "p"].to_numpy(dtype=float)
        )
    # Čistý walk-forward: objev jen na IS (BH přes IS p-hodnoty), OOS je pak nezávislé ověření
    tests["q_is"] = np.nan
    if "is_p" in tests.columns:
        has_is = tested & tests["is_p"].notna()
        tests.loc[has_is, "q_is"] = benjamini_hochberg(
            tests.loc[has_is, "is_p"].to_numpy(dtype=float)
        )

    def verdict(row: pd.Series) -> str:
        if not row["tested"] or pd.isna(row.get("p")):
            return "nedostatek dat"
        if row.get("predictor") == "size_rank_stability":
            # Test je sám srovnáním IS → OOS (pořadí mediánů) — vlastní IS fázi nemá
            if row["q"] > Q_PASS:
                return "bez efektu"
            oos_ok = not pd.isna(row["oos_p"]) and row["oos_p"] <= OOS_ALPHA
            return "prošlo" if oos_ok else "kandidát"
        # Objev jen z IS (q_is), směr z IS; OOS jen jednostranné ověření ve směru IS
        if pd.isna(row.get("q_is")):
            return "nedostatek dat"
        if row["q_is"] > Q_PASS:
            return "bez efektu"
        if _dir(row["oos_effect"]) != _dir(row["is_effect"]):
            return "nestabilní"
        if not pd.isna(row["oos_p"]) and row["oos_p"] <= OOS_ALPHA:
            return "prošlo"
        return "kandidát"

    tests["verdict"] = tests.apply(verdict, axis=1)
    return tests


# ── Report ─────────────────────────────────────────────────────────


def num(value: Any, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    return f"{value:.{digits}f}".replace(".", ",").replace("-", "−")


def signed(value: Any, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    return ("+" if value > 0 else "") + num(value, digits)


def pct(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    return f"{100 * value:.0f} %"


def pp(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    return ("+" if value > 0 else "") + f"{100 * value:.0f}".replace("-", "−") + " pb"


def ci(bounds: Any) -> str:
    if not isinstance(bounds, tuple) or any(math.isnan(b) for b in bounds):
        return "—"
    return f"{100 * bounds[0]:.0f}–{100 * bounds[1]:.0f}"


def pval(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    if value < 0.001:
        return f"{value:.0e}".replace("e-0", "e−").replace("e-", "e−")
    return f"{value:.3f}".replace(".", ",")


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """Markdown tabulka; svislítko v buňce (↓|+, |abn|) se escapuje, jinak by rozbilo sloupce."""

    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|")

    lines = ["| " + " | ".join(cell(h) for h in headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def unit_label(kind: str, unit: str) -> str:
    if kind in ("group", "all"):
        return GROUP_CZ.get(unit, unit)
    return unit


def s1_cell(row: pd.Series) -> str:
    if not row["tested"]:
        return "málo dat"
    return (
        f"↓|+ {pct(row['p_a'])} [{ci(row['ci_a'])}] ({row['n_a']:.0f}) · "
        f"↓|− {pct(row['p_b'])} [{ci(row['ci_b'])}] ({row['n_b']:.0f})"
    )


def oos_text(row: pd.Series) -> str:
    if pd.isna(row.get("oos_effect")):
        return "—"
    if row["predictor"] in ("surprise_abn_mw", "fomc_abn_mw"):
        return (
            f"IS {signed(row['is_effect'], 0)} bp → OOS {signed(row['oos_effect'], 0)} bp "
            f"(n {row['oos_n']:.0f}, p {pval(row['oos_p'])})"
        )
    hit = ""
    if not pd.isna(row.get("oos_hit")):
        hit = f", hit {pct(row['oos_hit'])} [{ci(row['oos_hit_ci'])}]"
    return (
        f"IS {pp(row['is_effect'])} → OOS {pp(row['oos_effect'])} "
        f"(n {row['oos_n']:.0f}{hit}, p {pval(row['oos_p'])})"
    )


def describe_signal(row: pd.Series) -> str:
    who = unit_label(row["unit_kind"], row["unit"])
    pred = row["predictor"]
    if pred == "surprise_sign":
        lean = "silnější/teplejší → pokles" if row["effect"] > 0 else "silnější/teplejší → růst"
        return f"{who}: {lean}"
    if pred == "fomc_cut_vs_hold":
        return f"FOMC: snížení → {'pokles' if row['effect'] > 0 else 'růst'} častěji než beze změny"
    if pred == "surprise_abn_mw":
        lean = "silnější/teplejší → nižší" if row["effect"] < 0 else "silnější/teplejší → vyšší"
        return f"{who}: abnormální pohyb {lean}"
    if pred == "drift":
        return f"{who}: nepodmíněně {'růst' if row['effect'] > 0 else 'pokles'} častěji"
    if pred in ("pre_60m", "pre_24h"):
        return f"{who}: {PREDICTOR_CZ[pred]} → {'pokračování' if row['effect'] > 0 else 'obrat'}"
    if pred == "vol_regime":
        return (
            f"{who}: ve vysoké vol. {'růst' if row['effect'] > 0 else 'pokles'} častěji než v nízké"
        )
    if pred == "prev_surprise":
        return f"{who}: minulé silnější/teplejší → {'pokles' if row['effect'] > 0 else 'růst'}"
    if pred.startswith("persist"):
        trend = "pokračuje" if row["effect"] > 0 else "se střídá"
        return f"{who}: překvapení {trend} ({PREDICTOR_CZ[pred]})"
    return f"{who}: {PREDICTOR_CZ.get(pred, pred)}"


def estimate_text(row: pd.Series) -> str:
    pred = row["predictor"]
    if pred in (
        "surprise_sign",
        "fomc_cut_vs_hold",
        "prev_surprise",
        "pre_60m",
        "pre_24h",
        "vol_regime",
    ) or pred.startswith("persist"):
        label_a, label_b = {
            "surprise_sign": ("P(↓|+)", "P(↓|−)"),
            "fomc_cut_vs_hold": ("P(↓|snížení)", "P(↓|beze změny)"),
            "prev_surprise": ("P(↓|minulé +)", "P(↓|minulé −)"),
            "pre_60m": ("P(↑|před ↑)", "P(↑|před ↓)"),
            "pre_24h": ("P(↑|před ↑)", "P(↑|před ↓)"),
            "vol_regime": ("P(↑|high)", "P(↑|low)"),
        }.get(pred, ("P(+|minulé +)", "P(+|minulé −)"))
        return (
            f"{label_a} {pct(row['p_a'])} (n {row['n_a']:.0f}), {label_b} {pct(row['p_b'])} "
            f"(n {row['n_b']:.0f}); pravidlo trefí {pct(row['hit'])}"
        )
    if pred in ("drift", "exc_vs_baseline"):
        return f"{pct(row['p_a'])} z {row['n_a']:.0f}"
    if pred in ("surprise_abn_mw", "fomc_abn_mw"):
        label_a, label_b = (
            ("snížení", "beze změny") if pred == "fomc_abn_mw" else ("nad odhadem", "pod odhadem")
        )
        return (
            f"medián abn {label_a} {signed(row['med_a'], 0)} bp (n {row['n_a']:.0f}), {label_b} "
            f"{signed(row['med_b'], 0)} bp (n {row['n_b']:.0f}); Δ {signed(row['effect'], 0)} bp"
        )
    if pred == "size_rank_stability":
        return f"ρ = {num(row['effect'], 2)} ({row['n_a']:.0f} řad)"
    return num(row["effect"], 2)


def interval_text(row: pd.Series) -> str:
    pred = row["predictor"]
    if pred in (
        "surprise_sign",
        "fomc_cut_vs_hold",
        "prev_surprise",
        "pre_60m",
        "pre_24h",
        "vol_regime",
    ) or pred.startswith("persist"):
        return f"[{ci(row['ci_a'])}] vs [{ci(row['ci_b'])}]; hit [{ci(row['hit_ci'])}]"
    if pred in ("drift", "exc_vs_baseline"):
        return f"[{ci(row['ci_a'])}]"
    return f"p {pval(row['p'])}, q {pval(row['q'])}"


def write_report(
    out_dir: Path,
    frame: pd.DataFrame,
    tests: pd.DataFrame,
    vol_tests: pd.DataFrame,
    sensitivity: pd.DataFrame,
    sizes: pd.DataFrame,
    stability: pd.DataFrame,
    cut: pd.Timestamp,
    history: pd.DataFrame,
    posthoc: pd.DataFrame,
) -> None:
    es = frame[frame["symbol"] == "ES"]
    lines: list[str] = []
    add = lines.append
    tested = tests[tests["tested"].fillna(False).astype(bool)]
    n_tests = len(tested)
    n_nominal = int((tested["p"] < 0.05).sum())
    passed = tested[tested["verdict"] == "prošlo"]
    candidates = tested[tested["verdict"] == "kandidát"]
    unstable = tested[tested["verdict"] == "nestabilní"]
    vol_tested = vol_tests[vol_tests["tested"].fillna(False).astype(bool)]

    add("# Předpověď pohybu ES/NQ po ohlášených releasech — kde je signál (#1296, fáze 2)")
    add("")
    add(
        f"Dataset: {es['event_id'].nunique()} releasů × ES/NQ ({es['ts_event'].min():%d. %m. %Y} – "
        f"{es['ts_event'].max():%d. %m. %Y}). Řez walk-forward: **{cut:%d. %m. %Y}** "
        "(IS = první 2/3 období, OOS = poslední 1/3). Skript "
        "`scripts/measure_release_predictability.py`."
    )
    add("")
    # Interpretace analytika (ručně psaná nad výsledky) — vloží se, když soubor existuje
    conclusions = out_dir / "conclusions1296.md"
    if conclusions.exists():
        add(conclusions.read_text(encoding="utf-8").strip())
        add("")
    add("## Souhrn čísel")
    add("")
    add(
        f"- Směrových testů (sekce 1, 3, 4): **{n_tests}**, nominálně p < 0,05: {n_nominal} "
        f"(čistá náhoda by dala ~{0.05 * n_tests:.0f}). Po Benjamini-Hochbergovi q ≤ 0,10: "
        f"{int((tested['q'] <= Q_PASS).sum())}, q ≤ 0,05: {int((tested['q'] <= 0.05).sum())}."
    )
    add(
        f"- **Prošlo** (IS q ≤ 0,10 + OOS ve směru IS s p ≤ 0,10): {len(passed)}; "
        f"kandidátů (OOS stejný směr, nepotvrzený): {len(candidates)}; nestabilních: "
        f"{len(unstable)}. Objevů jen z IS (q_is ≤ 0,10): "
        f"{int((tested['q_is'] <= Q_PASS).sum())}."
    )
    add(
        f"- Testy velikosti (vlastní rodina): {len(vol_tested)}, q ≤ 0,10: "
        f"{int((vol_tested['q'] <= Q_PASS).sum())}."
    )
    add("")
    add("Kritéria byla stanovena před pohledem na výsledky (docstring skriptu):")
    add("")
    add(
        "- **prošlo**: BH q ≤ 0,10 v celé rodině směrových testů počítané jen z IS p-hodnot "
        "(q_is), OOS ve směru IS s jednostranným p ≤ 0,10;"
    )
    add("- **kandidát**: q_is ≤ 0,10, OOS ve stejném směru, ale nepotvrzený;")
    add("- **nestabilní**: q_is ≤ 0,10, ale OOS opačně;")
    add(
        "- **bez efektu**: q_is > 0,10; **nedostatek dat**: < 5 releasů v některé buňce "
        "(netestuje se, do BH se nepočítá)."
    )
    add(
        "- Metodická korekce 26. 9. 2026 (ne ladění kritérií): první verze rozhodovala podle q "
        "z celého vzorku, který obsahuje OOS — OOS se tak použil k objevu i k ověření. q z celého "
        "vzorku je dál v tabulkách jen informativně."
    )
    add("")
    add("### Signály, které prošly")
    add("")
    if len(passed):
        add(
            table(
                [
                    "signál",
                    "symbol",
                    "horizont",
                    "odhad (celý vzorek)",
                    "95% interval",
                    "IS p / q_is",
                    "IS → OOS",
                ],
                (
                    [
                        describe_signal(r),
                        r["symbol"],
                        r["horizon"],
                        estimate_text(r),
                        interval_text(r),
                        f"{pval(r['is_p'])} / {pval(r['q_is'])}",
                        oos_text(r),
                    ]
                    for _, r in passed.sort_values(
                        ["section", "unit", "symbol", "horizon"]
                    ).iterrows()
                ),
            )
        )
    else:
        add("Žádný směrový signál kritéria nesplnil.")
    add("")
    add("### Kandidáti a nestabilní (IS q ≤ 0,10, ale OOS nepotvrdil)")
    add("")
    rest = pd.concat([candidates, unstable])
    if len(rest):
        add(
            table(
                ["verdikt", "signál", "symbol", "horizont", "odhad", "IS p / q_is", "IS → OOS"],
                (
                    [
                        r["verdict"],
                        describe_signal(r),
                        r["symbol"],
                        r["horizon"],
                        estimate_text(r),
                        f"{pval(r['is_p'])} / {pval(r['q_is'])}",
                        oos_text(r),
                    ]
                    for _, r in rest.sort_values(
                        ["verdict", "section", "unit", "symbol", "horizon"]
                    ).iterrows()
                ),
            )
        )
    else:
        add("Žádné.")
    add("")
    add("### Signály velikosti (vlastní rodina, q ≤ 0,10)")
    add("")
    vol_pass = vol_tested[vol_tested["verdict"].isin(["prošlo", "kandidát", "nestabilní"])]
    summary_rows = []
    for (symbol, h), part in vol_pass[vol_pass["predictor"] == "exc_vs_baseline"].groupby(
        ["symbol", "horizon"]
    ):
        ok = part[part["verdict"] == "prošlo"]
        summary_rows.append(
            [
                symbol,
                h,
                len(ok),
                ", ".join(
                    unit_label(k, u) for k, u in zip(ok["unit_kind"], ok["unit"], strict=True)
                ),
            ]
        )
    if summary_rows:
        add(
            table(
                ["symbol", "horizont", "prošlo", "jednotky (výchylka nad baseline stejné minuty)"],
                summary_rows,
            )
        )
    add("")
    for _, r in vol_tested[vol_tested["predictor"] == "size_rank_stability"].iterrows():
        add(
            f"- Stabilita pořadí typické velikosti řad IS → OOS, {r['symbol']} {r['horizon']}: "
            f"ρ = {num(r['effect'], 2)} ({r['n_a']:.0f} řad), p {pval(r['p'])}, q {pval(r['q'])} "
            f"→ {r['verdict']}"
        )
    add("")

    # ── 1. Podmíněný směr
    add("## 1. Podmíněný směr podle překvapení")
    add("")
    add(
        "`↓|+` = podíl poklesů po překvapení **nad odhadem** v ekonomickém smyslu (silnější "
        "ekonomika, "
        "teplejší inflace; u nezaměstnanosti a Claims je to nižší číslo), `↓|−` = po překvapení "
        "pod "
        "odhadem. V hranatých závorkách Wilsonův 95% interval v %, v kulatých n. Krátké "
        "horizonty = "
        "surový výnos od minuty před releasem, 1d–10d = abnormální pohyb (proti baseline ±60 "
        "seancí). "
        "Nulová překvapení a nulové výnosy se do podílů nepočítají. Skupiny = jeden headline "
        "řádek na "
        "shluk a skupinu (`primary_in_group`)."
    )
    add("")
    s1 = tests[(tests["section"] == "S1") & (tests["predictor"] == "surprise_sign")]
    for kind, title in (("group", "### Skupiny"), ("series", "### Klíčové řady")):
        add(title)
        add("")
        part = s1[s1["unit_kind"] == kind]
        if kind == "series":
            part = part[part["unit"].isin(KEY_SERIES)]
        order = GROUPS if kind == "group" else KEY_SERIES
        body: list[list[Any]] = []
        for unit in order:
            for h in HORIZONS:
                cells: dict[str, pd.Series] = {}
                verdict_bits = []
                for symbol in SYMBOLS:
                    match = part[
                        (part["unit"] == unit) & (part["horizon"] == h) & (part["symbol"] == symbol)
                    ]
                    if match.empty:
                        continue
                    r = match.iloc[0]
                    cells[symbol] = r
                    if r["tested"]:
                        verdict_bits.append(f"{symbol}: {r['verdict']}")
                if not cells:
                    continue
                if not any(r["tested"] for r in cells.values()):
                    if h == HORIZONS[0]:
                        body.append(
                            [
                                unit_label(kind, unit),
                                "vše",
                                "málo dat (< 5 v buňce)",
                                "",
                                "",
                                "",
                                "",
                            ]
                        )
                    continue
                row = [unit_label(kind, unit), h]
                for symbol in SYMBOLS:
                    r = cells.get(symbol)
                    if r is None or not r["tested"]:
                        row += ["málo dat", ""]
                    else:
                        row += [s1_cell(r), f"{pval(r['p'])} / {pval(r['q'])}"]
                row.append("; ".join(verdict_bits))
                body.append(row)
        add(table(["jednotka", "horizont", "ES", "ES p / q", "NQ", "NQ p / q", "verdikt"], body))
        add("")
    add("### FOMC")
    add("")
    fomc = tests[(tests["predictor"] == "fomc_cut_vs_hold")]
    add(
        "FF forecast sazby se trefil v 17 z 18 rozhodnutí, znaménko překvapení je tedy bezcenné. "
        "Místo něj rozhodnutí: snížení (n = "
        f"{int((es['fomc_change'] < 0).sum())}) vs. beze změny (n = "
        f"{int((es['fomc_change'] == 0).sum())}); "
        f"zvýšení {int((es['fomc_change'] > 0).sum())}×. Rozhodnutí je ale očekávané, není to "
        "překvapení."
    )
    add("")
    add(
        table(
            ["symbol", "horizont", "P(↓|snížení)", "P(↓|beze změny)", "p / q", "verdikt"],
            (
                [
                    r["symbol"],
                    r["horizon"],
                    f"{pct(r['p_a'])} [{ci(r['ci_a'])}] ({r['n_a']:.0f})"
                    if r["tested"]
                    else "málo dat",
                    f"{pct(r['p_b'])} [{ci(r['ci_b'])}] ({r['n_b']:.0f})" if r["tested"] else "",
                    f"{pval(r.get('p'))} / {pval(r.get('q'))}",
                    r["verdict"],
                ]
                for _, r in fomc.iterrows()
            ),
        )
    )
    add("")
    fomc_rows = es[es["series"] == FOMC]
    add(
        "Nepodmíněně po FOMC (ES): "
        + ", ".join(
            f"{h} ↓ "
            f"{int((fomc_rows[move_col(h)] < 0).sum())}/{int(fomc_rows[move_col(h)].notna().sum())}"
            for h in HORIZONS
        )
        + "."
    )
    add("")
    add("### Síla překvapení u testovaných signálů (popisné, mimo BH)")
    add("")
    strong = tested[
        (tested["predictor"] == "surprise_sign")
        & tested["verdict"].isin(["prošlo", "kandidát", "nestabilní"])
    ]
    if len(strong):
        body = []
        units_by_symbol = {symbol: unit_frames(frame, symbol) for symbol in SYMBOLS}
        for _, r in strong.iterrows():
            sub = units_by_symbol[r["symbol"]][(r["unit_kind"], r["unit"])]
            values = sub[move_col(r["horizon"])].to_numpy(dtype=float)
            econ = sub["surprise_econ_sign"].to_numpy(dtype=float)
            z = sub["surprise_z_pit"].abs().to_numpy(dtype=float)
            valid = ~np.isnan(values) & (values != 0)
            splits: list[str] = []
            for label, zmask in (("|z| < 0,5", z < BIG_Z), ("|z| ≥ 0,5", z >= BIG_Z)):
                a = valid & zmask & (econ > 0)
                b = valid & zmask & (econ < 0)
                splits.append(
                    f"{label}: ↓|+ {int((values[a] < 0).sum())}/{int(a.sum())}, ↓|− "
                    f"{int((values[b] < 0).sum())}/{int(b.sum())}"
                )
            med_a = (
                np.median(values[valid & (econ > 0)]) if (valid & (econ > 0)).any() else math.nan
            )
            med_b = (
                np.median(values[valid & (econ < 0)]) if (valid & (econ < 0)).any() else math.nan
            )
            body.append(
                [
                    unit_label(r["unit_kind"], r["unit"]),
                    r["symbol"],
                    r["horizon"],
                    splits[0],
                    splits[1],
                    f"{signed(med_a, 0)} / {signed(med_b, 0)}",
                ]
            )
        add(
            table(
                [
                    "jednotka",
                    "symbol",
                    "horizont",
                    "malé překvapení",
                    "velké překvapení",
                    "medián bp (+ / −)",
                ],
                body,
            )
        )
    else:
        add("Žádný signál k rozpadu.")
    add("")

    # ── 2. Velikost
    add("## 2. Velikost pohybu")
    add("")
    add(
        "Krátké horizonty: `výchylka` = max(|nahoru|, |dolů|) od close minuty před releasem, "
        "`abn` = "
        "|abnormální výnos|, `pct` = medián percentilu výchylky proti stejné minutě dne v ±60 "
        "seancích bez "
        "události (50 = běžný den). Vícedenní: |výnos| a |abnormální výnos|. Hodnoty v bp: "
        "medián / p75."
    )
    add("")
    for symbol in SYMBOLS:
        add(f"### {symbol} — skupiny a klíčové řady, všechny režimy")
        add("")
        part = sizes[(sizes["symbol"] == symbol) & (sizes["regime"] == "vše")]
        body = []
        for kind, unit in [("group", g) for g in GROUPS] + [("series", s) for s in KEY_SERIES]:
            rows = part[(part["unit_kind"] == kind) & (part["unit"] == unit)].set_index("horizon")
            if rows.empty or rows["n"].max() == 0:
                continue
            row = [unit_label(kind, unit), f"{rows.loc['5m', 'n']:.0f}"]
            for h in SHORT_H:
                r = rows.loc[h]
                row.append(
                    f"{num(r['size_med'], 0)} / {num(r['size_p75'], 0)} (pct "
                    f"{num(r['exc_pct_med'], 0)})"
                )
            for h in ("1d", "5d", "10d"):
                r = rows.loc[h]
                row.append(f"{num(r['abn_med'], 0)} / {num(r['abn_p75'], 0)}")
            body.append(row)
        add(
            table(
                [
                    "jednotka",
                    "n",
                    "5m výchylka",
                    "15m výchylka",
                    "60m výchylka",
                    "1d |abn|",
                    "5d |abn|",
                    "10d |abn|",
                ],
                body,
            )
        )
        add("")
    add("### Podle volatilitního režimu (tercil rv20 z celého vzorku), skupiny")
    add("")
    for symbol in SYMBOLS:
        part = sizes[(sizes["symbol"] == symbol) & (sizes["unit_kind"] == "group")]
        body = []
        for group in GROUPS:
            for h in ("5m", "15m", "60m", "1d", "5d"):
                row = [GROUP_CZ[group], h]
                for regime in ("low", "mid", "high"):
                    r = part[
                        (part["unit"] == group)
                        & (part["regime"] == regime)
                        & (part["horizon"] == h)
                    ]
                    if r.empty or r.iloc[0]["n"] == 0:
                        row.append("—")
                        continue
                    r0 = r.iloc[0]
                    metric = ("size_med", "size_p75") if h in SHORT_H else ("abn_med", "abn_p75")
                    row.append(
                        f"{num(r0[metric[0]], 0)} / {num(r0[metric[1]], 0)} (n {r0['n']:.0f})"
                    )
                body.append(row)
        add(f"**{symbol}** (krátké = výchylka, vícedenní = |abn|; medián / p75 bp)")
        add("")
        add(table(["skupina", "horizont", "low", "mid", "high"], body))
        add("")
    add("### Klíčové řady podle režimu (15m výchylka a 1d |abn|, medián / p75 bp)")
    add("")
    for symbol in SYMBOLS:
        part = sizes[(sizes["symbol"] == symbol) & (sizes["unit_kind"] == "series")]
        body = []
        for series in KEY_SERIES:
            row = [series]
            for h in ("15m", "1d"):
                for regime in ("low", "mid", "high"):
                    r = part[
                        (part["unit"] == series)
                        & (part["regime"] == regime)
                        & (part["horizon"] == h)
                    ]
                    if r.empty or r.iloc[0]["n"] == 0:
                        row.append("—")
                        continue
                    r0 = r.iloc[0]
                    metric = ("size_med", "size_p75") if h in SHORT_H else ("abn_med", "abn_p75")
                    row.append(f"{num(r0[metric[0]], 0)} / {num(r0[metric[1]], 0)} ({r0['n']:.0f})")
            body.append(row)
        add(f"**{symbol}**")
        add("")
        add(table(["řada", "15m low", "15m mid", "15m high", "1d low", "1d mid", "1d high"], body))
        add("")
    add("### Stabilita typické velikosti IS → OOS (medián |výchylky| 15m / |abn| 1d, bp)")
    add("")
    body = []
    for series in KEY_SERIES:
        row = [series]
        for symbol in SYMBOLS:
            for h in ("15m", "1d"):
                r = stability[
                    (stability["symbol"] == symbol)
                    & (stability["unit"] == series)
                    & (stability["horizon"] == h)
                ]
                if r.empty:
                    row.append("—")
                    continue
                r0 = r.iloc[0]
                row.append(
                    f"{num(r0['is_med'], 0)} ({r0['is_n']:.0f}) → {num(r0['oos_med'], 0)} "
                    f"({r0['oos_n']:.0f})"
                )
        body.append(row)
    add(table(["řada", "ES 15m", "ES 1d", "NQ 15m", "NQ 1d"], body))
    add("")
    exc = vol_tested[vol_tested["predictor"] == "exc_vs_baseline"]
    add("### Je výchylka po releasu větší než v téže minutě běžného dne? (řady, q ≤ 0,10 a OOS)")
    add("")
    body = []
    for series in sorted(exc["unit"].unique()):
        row = [unit_label("series" if series not in GROUPS else "group", series)]
        for symbol in SYMBOLS:
            for h in SHORT_H:
                r = exc[(exc["unit"] == series) & (exc["symbol"] == symbol) & (exc["horizon"] == h)]
                if r.empty:
                    row.append("—")
                    continue
                r0 = r.iloc[0]
                mark = {"prošlo": "✔", "kandidát": "~", "nestabilní": "!", "bez efektu": "·"}.get(
                    r0["verdict"], "?"
                )
                row.append(f"{mark} {pct(r0['p_a'])} ({r0['n_a']:.0f})")
        body.append(row)
    add(
        "`✔` prošlo, `~` kandidát, `!` nestabilní, `·` bez efektu; číslo = podíl releasů s "
        "výchylkou nad mediánem baseline."
    )
    add("")
    add(table(["jednotka", "ES 5m", "ES 15m", "ES 60m", "NQ 5m", "NQ 15m", "NQ 60m"], body))
    add("")

    # ── 3. Sklon před releasem
    add("## 3. Nepodmíněný sklon před releasem")
    add("")
    add(
        "Co je známé před zveřejněním: nepodmíněný sklon (P(růst) ≠ 50 %), pohyb 60 min a 24 h "
        "před "
        "releasem (pokračování vs. obrat), volatilitní režim point-in-time (high vs. low tercil), "
        "minulé překvapení téže řady. U každé jednotky n a rozdíl podílů; verdikt po BH v celé "
        "rodině."
    )
    add("")
    s3 = tested[(tested["section"] == "S3") & ~tested["predictor"].str.startswith("persist")]
    body = []
    for pred in ("drift", "pre_60m", "pre_24h", "vol_regime", "prev_surprise"):
        part = s3[s3["predictor"] == pred]
        if part.empty:
            continue
        best = part.nsmallest(3, "p")
        body.append(
            [
                PREDICTOR_CZ[pred],
                len(part),
                int((part["p"] < 0.05).sum()),
                num(0.05 * len(part)),
                int((part["q"] <= Q_PASS).sum()),
                "; ".join(
                    f"{unit_label(r['unit_kind'], r['unit'])} {r['symbol']} {r['horizon']}: "
                    f"{pp(r['effect'])} (p {pval(r['p'])}, q {pval(r['q'])}, {r['verdict']})"
                    for _, r in best.iterrows()
                ),
            ]
        )
    add(
        table(
            ["prediktor", "testů", "p < 0,05", "čekáno náhodou", "q ≤ 0,10", "nejsilnější 3"], body
        )
    )
    add("")
    add("### Skupiny a vše — podrobně (5m a 60m)")
    add("")
    body = []
    for pred in ("drift", "pre_60m", "pre_24h", "vol_regime", "prev_surprise"):
        part = s3[
            (s3["predictor"] == pred)
            & s3["unit_kind"].isin(["all", "group"])
            & s3["horizon"].isin(["5m", "60m"])
        ]
        for _, r in part.iterrows():
            if pred == "drift":
                est = f"P(↑) {pct(r['p_a'])} [{ci(r['ci_a'])}] (n {r['n_a']:.0f})"
            else:
                est = (
                    f"{pct(r['p_a'])} [{ci(r['ci_a'])}] ({r['n_a']:.0f}) vs {pct(r['p_b'])} "
                    f"[{ci(r['ci_b'])}] ({r['n_b']:.0f})"
                )
            body.append(
                [
                    PREDICTOR_CZ[pred],
                    unit_label(r["unit_kind"], r["unit"]),
                    r["symbol"],
                    r["horizon"],
                    est,
                    f"{pval(r['p'])} / {pval(r['q'])}",
                    r["verdict"],
                ]
            )
    add(table(["prediktor", "jednotka", "symbol", "horizont", "odhad", "p / q", "verdikt"], body))
    add("")
    add(
        "Pro `pohyb před` a `vol. režim` je odhad P(růst | podmínka A) vs. P(růst | podmínka B); "
        "pro `minulé překvapení` P(pokles | minulé silnější) vs. P(pokles | minulé slabší)."
    )
    add("")
    add("### Perzistence překvapení (předpoví minulé překvapení řady to příští?)")
    add("")
    persist = tested[tested["predictor"].str.startswith("persist")]
    add(
        f"Historie překvapení z PG (všechny releasy řad s actual i forecast, libovolný impact): "
        f"{len(history)} releasů od {history['ts'].min():%d. %m. %Y}; řez IS/OOS stejný jako "
        "jinde. "
        f"Testů {len(persist)}, p < 0,05: {int((persist['p'] < 0.05).sum())} "
        f"(náhodou ~{num(0.05 * len(persist))}), q ≤ 0,10: {int((persist['q'] <= Q_PASS).sum())}."
    )
    add("")
    body = []
    for _, r in (
        persist[(persist["unit_kind"] == "group") | (persist["unit"].isin(KEY_SERIES))]
        .sort_values(["predictor", "unit_kind", "unit"])
        .iterrows()
    ):
        body.append(
            [
                PREDICTOR_CZ[r["predictor"]],
                unit_label(r["unit_kind"], r["unit"]),
                f"{pct(r['p_a'])} [{ci(r['ci_a'])}] ({r['n_a']:.0f})",
                f"{pct(r['p_b'])} [{ci(r['ci_b'])}] ({r['n_b']:.0f})",
                f"{pval(r['p'])} / {pval(r['q'])}",
                oos_text(r),
                r["verdict"],
            ]
        )
    add(
        table(
            ["test", "jednotka", "P(+|minulé +)", "P(+|minulé −)", "p / q", "IS → OOS", "verdikt"],
            body,
        )
    )
    add("")
    gex_all = frame[frame["gex_regime"].notna()]
    gex_es = gex_all[gex_all["symbol"] == "ES"]
    add("### GEX režim (jen popisně)")
    add("")
    add(
        f"GEX režim je u {gex_es['event_id'].nunique()} releasů ES "
        f"({gex_es['cluster_ts'].nunique()} shluků, "
        f"od {gex_es['ts_event'].min():%d. %m. %Y}) — celé až po řezu IS/OOS, takže walk-forward "
        "nejde a "
        "buňky jsou malé. Netestuje se; tabulka je jen orientační (headline shluku):"
    )
    add("")
    body = []
    for (symbol, regime), part in gex_all[gex_all["primary_in_cluster"]].groupby(
        ["symbol", "gex_regime"]
    ):
        up = part["ret_60m_bp"].dropna()
        body.append(
            [
                symbol,
                regime,
                len(part),
                num(part["exc_5m_bp"].median(), 0),
                num(part["exc_15m_bp"].median(), 0),
                num(part["exc_60m_bp"].median(), 0),
                num(part["exc_pct_15m"].median(), 0),
                f"{int((up > 0).sum())}/{len(up)}",
            ]
        )
    add(
        table(
            [
                "symbol",
                "režim",
                "shluků",
                "5m výchylka",
                "15m výchylka",
                "60m výchylka",
                "15m pct",
                "60m ↑",
            ],
            body,
        )
    )
    add("")

    # ── 4. Vícedenní
    add("## 4. Vícedenní abnormální pohyb podle překvapení")
    add("")
    add(
        "Abnormální pohyb = výnos od releasu do settle N-té seance minus medián baseline (start "
        "ve stejnou "
        "minutu dne v ±60 seancích bez High události). Medián pro překvapení + a −, rozdíl, "
        "Mann-Whitney "
        "(oboustranný). Okna se u týdenních řad (Claims) překrývají — Wilsonovy intervaly pak "
        "přeceňují "
        "přesnost, test na permutaci znamének překvapení ale zůstává platný."
    )
    add("")
    s4 = tests[(tests["section"] == "S4")]
    body = []
    for kind, unit in [("group", g) for g in GROUPS] + [("series", s) for s in KEY_SERIES]:
        for h in DAILY_H:
            row = [unit_label(kind, unit), h]
            any_tested = False
            for symbol in SYMBOLS:
                match = s4[
                    (s4["unit_kind"] == kind)
                    & (s4["unit"] == unit)
                    & (s4["horizon"] == h)
                    & (s4["symbol"] == symbol)
                ]
                if match.empty or not match.iloc[0]["tested"]:
                    row += ["málo dat", ""]
                    continue
                any_tested = True
                r = match.iloc[0]
                row += [
                    f"{signed(r['med_a'], 0)} ({r['n_a']:.0f}) / {signed(r['med_b'], 0)} "
                    f"({r['n_b']:.0f})",
                    f"{pval(r['p'])} / {pval(r['q'])} {r['verdict']}",
                ]
            if any_tested:
                body.append(row)
    add(
        table(
            [
                "jednotka",
                "horizont",
                "ES medián abn + / − (n)",
                "ES p / q",
                "NQ medián abn + / − (n)",
                "NQ p / q",
            ],
            body,
        )
    )
    add("")
    add("### Citlivost na překryv (skupiny; Δ = rozdíl mediánů + minus −, bp, n + / −)")
    add("")
    counts_rows = []
    for h in DAILY_H:
        base = es[es[move_col(h)].notna()]
        counts_rows.append(
            [
                h,
                len(base),
                int((~as_bool(base[f"overlap_fomc_{h}"])).sum()),
                int((base[f"overlap_t1_{h}"] == 0).sum()),
                int((base[f"overlap_n_{h}"] == 0).sum()),
            ]
        )
    add(
        table(["horizont", "oken (ES)", "bez FOMC", "bez tier-1", "striktně bez High"], counts_rows)
    )
    add("")
    add("Tier-1 = Core CPI, Core PPI, Core PCE, NFP, FOMC. Rozdíl mediánů při vyřazení oken:")
    add("")
    body = []
    sens = sensitivity[
        (sensitivity["unit_kind"] == "group") & (sensitivity["predictor"] == "surprise_abn_mw")
    ]
    for group in GROUPS:
        for h in DAILY_H:
            row = [GROUP_CZ[group], h]
            present = False
            for symbol in SYMBOLS:
                base = s4[
                    (s4["unit"] == group)
                    & (s4["unit_kind"] == "group")
                    & (s4["horizon"] == h)
                    & (s4["symbol"] == symbol)
                ]
                if base.empty or not base.iloc[0]["tested"]:
                    row.append("—")
                else:
                    present = True
                    b0 = base.iloc[0]
                    row.append(f"{signed(b0['effect'], 0)} (p {pval(b0['p'])})")
                for spec in ("bez FOMC", "bez tier-1", "striktně bez High"):
                    s = sens[
                        (sens["unit"] == group)
                        & (sens["horizon"] == h)
                        & (sens["symbol"] == symbol)
                        & (sens["spec"] == spec)
                    ]
                    if s.empty:
                        row.append("—")
                        continue
                    s0 = s.iloc[0]
                    if pd.isna(s0.get("effect")):
                        row.append(f"málo ({s0['n_a']:.0f}/{s0['n_b']:.0f})")
                    else:
                        row.append(
                            f"{signed(s0['effect'], 0)} (p {pval(s0['p'])}; "
                            f"{s0['n_a']:.0f}/{s0['n_b']:.0f})"
                        )
            if present:
                body.append(row)
    add(
        table(
            [
                "skupina",
                "h",
                "ES vše",
                "ES bez FOMC",
                "ES bez tier-1",
                "ES striktně",
                "NQ vše",
                "NQ bez FOMC",
                "NQ bez tier-1",
                "NQ striktně",
            ],
            body,
        )
    )
    add("")
    flagged = tested[tested["horizon"].isin(DAILY_H) & (tested["q"] <= Q_PASS)]
    if len(flagged):
        add("Vícedenní testy s q ≤ 0,10 a jejich citlivost na překryv:")
        add("")
        body = []
        for _, r in flagged.iterrows():
            s = sensitivity[
                (sensitivity["unit"] == r["unit"])
                & (sensitivity["unit_kind"] == r["unit_kind"])
                & (sensitivity["horizon"] == r["horizon"])
                & (sensitivity["symbol"] == r["symbol"])
                & (sensitivity["predictor"] == r["predictor"])
            ]
            parts = []
            for _, s0 in s.iterrows():
                if pd.isna(s0.get("effect")):
                    parts.append(f"{s0['spec']}: málo dat")
                else:
                    value = (
                        pp(s0["effect"])
                        if r["predictor"] in ("surprise_sign", "fomc_cut_vs_hold")
                        else f"{signed(s0['effect'], 0)} bp"
                    )
                    parts.append(f"{s0['spec']}: {value} (p {pval(s0['p'])})")
            body.append(
                [describe_signal(r), r["symbol"], r["horizon"], r["verdict"], "; ".join(parts)]
            )
        add(table(["signál", "symbol", "h", "verdikt", "citlivost"], body))
        add("")

    # ── 5. Walk-forward
    add("## 5. Walk-forward (IS první 2/3 → OOS poslední 1/3)")
    add("")
    add(
        f"Řez {cut:%d. %m. %Y}. IS vícedenní okna končící po řezu vyřazena (purge). Pro každý "
        "test: směr "
        "v IS, OOS jednostranný test v tomto směru. Tabulka ukazuje čistý walk-forward — výběr jen "
        "podle IS p-hodnoty (OOS se výběru neúčastní), pak kolik vybraných drží znaménko a "
        "projde OOS. "
        "Bez signálu je shoda znaménka ~50 % a OOS p ≤ 0,10 u ~10 % (u malých OOS buněk méně)."
    )
    add("")
    body = []
    for section, label in (
        ("S1", "1 podmíněný směr"),
        ("S3", "3 sklon před releasem"),
        ("S4", "4 vícedenní abn"),
    ):
        part = tested[
            (tested["section"] == section)
            & tested["is_effect"].notna()
            & tested["oos_effect"].notna()
        ]
        part = part[(part["is_effect"] != 0) & (part["oos_effect"] != 0)]
        wf_row: list[Any] = [label]
        for name, chosen in (
            ("vše", part),
            ("IS p < 0,05", part[part["is_p"] < 0.05]),
            ("IS BH q ≤ 0,10", part[part["q_is"] <= Q_PASS]),
        ):
            if not len(chosen):
                wf_row.append(f"{name}: 0")
                continue
            agree = (np.sign(chosen["is_effect"]) == np.sign(chosen["oos_effect"])).mean()
            confirmed = int((chosen["oos_p"] <= OOS_ALPHA).sum())
            wf_row.append(f"{len(chosen)} → shoda {pct(agree)}, OOS p ≤ 0,10: {confirmed}")
        body.append(wf_row)
    add(
        table(
            ["sekce", "všechny testy s IS i OOS", "vybrané IS p < 0,05", "objevy IS (BH q ≤ 0,10)"],
            body,
        )
    )
    add("")
    discoveries = tested[tested["q_is"] <= Q_PASS].sort_values("is_p")
    if len(discoveries):
        add("Objevy jen z IS (BH přes IS p-hodnoty celé rodiny) a jejich OOS:")
        add("")
        add(
            table(
                ["signál", "symbol", "h", "IS p / q", "IS → OOS", "verdikt"],
                (
                    [
                        describe_signal(r),
                        r["symbol"],
                        r["horizon"],
                        f"{pval(r['is_p'])} / {pval(r['q_is'])}",
                        oos_text(r),
                        r["verdict"],
                    ]
                    for _, r in discoveries.iterrows()
                ),
            )
        )
        add("")
    add("Podmíněný směr — skupiny, 5m/15m/60m, IS → OOS (rozdíl P(↓|+) − P(↓|−)):")
    add("")
    body = []
    part = s1[(s1["unit_kind"] == "group") & s1["tested"] & s1["horizon"].isin(SHORT_H + ("1d",))]
    for _, r in part.iterrows():
        body.append(
            [
                GROUP_CZ[r["unit"]],
                r["symbol"],
                r["horizon"],
                pp(r["effect"]),
                oos_text(r),
                r["verdict"],
            ]
        )
    add(table(["skupina", "symbol", "h", "celý vzorek Δ", "IS → OOS", "verdikt"], body))
    add("")

    # ── 5b. Post-hoc
    add("## 5b. Post-hoc: jádro inflace a síla překvapení (mimo rodinu, mimo verdikty)")
    add("")
    add(
        "Vzniklo **po pohledu na OOS**: inflační skupina v OOS slábla a vzor rozbíjely hlavně "
        "řádky "
        "ISM Manufacturing Prices — ty jsou v inflační skupině headline, ale vycházejí současně "
        "s ISM PMI "
        "(růst) a trh reaguje na něj. Tady je jednotka headline **shluku** (`primary_in_cluster`) "
        "s inflační řadou, tj. prakticky jen Core CPI, Core PPI a Core PCE. OOS už proto není "
        "čisté "
        "ověření — výsledek je hypotéza pro ověřování na nových datech (učící smyčka fáze 4), ne "
        "prošlý signál. "
        f"Post-hoc pohledů: {len(posthoc)}; p-hodnoty nekorigované."
    )
    add("")
    if len(posthoc):
        direction = posthoc[posthoc["kind"] == "směr"]
        body = []
        for _, r in direction.iterrows():
            body.append(
                [
                    r["symbol"],
                    r["horizon"],
                    r["z_split"],
                    f"{pct(r['p_a'])} [{ci(r['ci_a'])}] ({r['n_a']:.0f})",
                    f"{pct(r['p_b'])} [{ci(r['ci_b'])}] ({r['n_b']:.0f})",
                    f"{pct(r['hit'])} [{ci(r['hit_ci'])}]",
                    pval(r["p"]),
                    oos_text(pd.Series({**r.to_dict(), "predictor": "surprise_sign"})),
                ]
            )
        add(
            table(
                [
                    "symbol",
                    "h",
                    "překvapení",
                    "P(↓|teplejší)",
                    "P(↓|chladnější)",
                    "pravidlo trefí",
                    "p",
                    "IS → OOS",
                ],
                body,
            )
        )
        add("")
        abn = posthoc[posthoc["kind"].isin(["abn MW", "abn MW epizody"])]
        if len(abn):
            add(
                "Vícedenní abnormální pohyb jádra inflace (medián bp, teplejší vs. chladnější). "
                "Řádky "
                f"`epizody`: CPI, PPI a PCE do {EPISODE_DAYS} dní od sebe sloučené do jednoho "
                "pozorování "
                "(znaménko součtu překvapení, okno od prvního releasu) — jejich vícedenní okna "
                "se překrývají "
                "a překvapení korelují, takže p-hodnoty po releasech přeceňují počet nezávislých "
                "pozorování:"
            )
            add("")
            add(
                table(
                    ["symbol", "h", "jednotka", "teplejší", "chladnější", "Δ", "p", "IS → OOS"],
                    (
                        [
                            r["symbol"],
                            r["horizon"],
                            "releasy" if r["kind"] == "abn MW" else r["z_split"],
                            f"{signed(r['med_a'], 0)} ({r['n_a']:.0f})",
                            f"{signed(r['med_b'], 0)} ({r['n_b']:.0f})",
                            signed(r["effect"], 0),
                            pval(r["p"]),
                            oos_text(pd.Series({**r.to_dict(), "predictor": "surprise_abn_mw"})),
                        ]
                        for _, r in abn.iterrows()
                    ),
                )
            )
            add("")

    # ── 6. Vícenásobné testování
    add("## 6. Vícenásobné testování")
    add("")
    body = []
    for (section, pred), part in tested.groupby(["section", "predictor"]):
        body.append(
            [
                section,
                PREDICTOR_CZ.get(pred, pred),
                len(part),
                int((part["p"] < 0.05).sum()),
                num(0.05 * len(part)),
                int((part["q"] <= Q_PASS).sum()),
                int((part["q_section"] <= Q_PASS).sum()),
                int((part["verdict"] == "prošlo").sum()),
            ]
        )
    body.append(
        [
            "Σ",
            "směrová rodina",
            n_tests,
            n_nominal,
            num(0.05 * n_tests),
            int((tested["q"] <= Q_PASS).sum()),
            "",
            len(passed),
        ]
    )
    add(
        table(
            [
                "sekce",
                "test",
                "počet",
                "p < 0,05",
                "čekáno náhodou",
                "BH q ≤ 0,10 (rodina)",
                "BH q ≤ 0,10 (jen sekce)",
                "prošlo",
            ],
            body,
        )
    )
    add("")
    untested = tests[~tests["tested"].fillna(False).astype(bool)]
    add(
        f"Netestováno pro nedostatek dat (< {MIN_CELL} v buňce): {len(untested)} kombinací "
        f"(nezapočteny do BH). Rodina velikosti: {len(vol_tested)} testů, BH zvlášť."
    )
    add("")
    add(
        "BH předpokládá nezávislé nebo kladně závislé testy. Testy jsou silně kladně závislé "
        "(CPI m/m "
        "a Core CPI sdílí reakci, 5m a 15m se překrývají, ES a NQ korelují ~0,9), takže "
        "efektivní počet "
        "nezávislých hypotéz je menší a BH je spíš konzervativní."
    )
    add("")

    # ── 7. Kde signál NENÍ
    add("## 7. Kde signál není")
    add("")
    no_signal = no_signal_lines(tests, vol_tests)
    lines.extend(f"- {line}" for line in no_signal)
    add("")
    add("## 8. Omezení")
    add("")
    for line in LIMITATIONS:
        add(f"- {line}")
    add("")
    (out_dir / "report1296.md").write_text("\n".join(lines), encoding="utf-8")


LIMITATIONS = (
    "Malé n: řada má ~23–26 releasů, po rozdělení na +/− a IS/OOS jsou OOS buňky 3–5 releasů. "
    "Jednostranný OOS test na takové buňce dosáhne p ≤ 0,10 jen při téměř dokonalé shodě — OOS "
    "potvrzení na úrovni řady je proto slabé; spolehlivější je úroveň skupiny.",
    "Souběžné releasy (CPI m/m + Core + y/y, Claims s Retail Sales / Philly) nesou tutéž reakci; "
    "u řady, která v shluku není headline, měří test reakci na celý shluk, ne na tu řadu.",
    "Krátká okna 60 min obsahují i další události (10:00 ISM po 8:30 datech) — kontaminace "
    "přidává šum, ne zkreslení znaménka.",
    "Vícedenní okna se překrývají s dalšími High releasy téměř vždy (10 seancí: všechna). "
    "Striktní vyřazení je proveditelné jen do 1–2 seancí; 5d a 10d signály nelze od dalších "
    "událostí oddělit.",
    "Období 7/2024–9/2026 je převážně býčí s několika šoky (8/2024, 12/2024 FOMC, 4/2025 cla). "
    "Abnormální pohyb odečítá medián baseline, ale extrémy (4/2025) dominují p75.",
    "`vol_tercile_pit` existuje až od ~12/2024, IS pro režim je proto kratší; v tabulkách "
    "velikosti "
    "je tercil z celého vzorku (popisný, ne point-in-time).",
    "FOMC: překvapení z FF forecastu je nulové 17× z 18 — podmínka „snížení vs. beze změny“ "
    "není překvapení a projevy/dot plot data nemají.",
    "GEX režim historicky neexistuje (jen od 7/2026, celý v OOS) — prediktivní hodnota GEX pro "
    "směr po releasu zatím změřit nejde.",
)


def no_signal_lines(tests: pd.DataFrame, vol_tests: pd.DataFrame) -> list[str]:
    tested = tests[tests["tested"].fillna(False).astype(bool)]
    out = []
    s1 = tested[tested["predictor"] == "surprise_sign"]
    for kind, label in (("group", "skupiny"), ("series", "řady")):
        part = s1[s1["unit_kind"] == kind]
        dead = []
        for unit, rows in part.groupby("unit"):
            if (rows["verdict"] == "bez efektu").all():
                dead.append(unit_label(kind, unit))
        if dead:
            out.append(
                f"Podmíněný směr podle znaménka překvapení, {label} bez efektu na žádném "
                "horizontu ES ani NQ: " + ", ".join(sorted(dead)) + "."
            )
    for h_set, label in ((DAILY_H, "vícedenní (1–10 seancí)"), (SHORT_H, "krátké (5–60 min)")):
        part = s1[s1["horizon"].isin(h_set)]
        if len(part) and not (part["verdict"] == "prošlo").any():
            out.append(f"Podmíněný směr — {label}: žádný signál neprošel ({len(part)} testů).")
    s4 = tested[tested["section"] == "S4"]
    if len(s4) and not (s4["verdict"] == "prošlo").any():
        out.append(
            f"Vícedenní abnormální pohyb podle překvapení (MW): žádný signál neprošel ({len(s4)} "
            "testů)."
        )
    s3 = tested[tested["section"] == "S3"]
    for pred in (
        "drift",
        "pre_60m",
        "pre_24h",
        "vol_regime",
        "prev_surprise",
        "persist_prev1",
        "persist_prev3",
    ):
        part = s3[s3["predictor"] == pred]
        if len(part) and not (part["verdict"] == "prošlo").any():
            out.append(
                f"{PREDICTOR_CZ[pred][0].upper()}{PREDICTOR_CZ[pred][1:]}: bez prokazatelného "
                "efektu "
                f"({len(part)} testů, p < 0,05 u {int((part['p'] < 0.05).sum())}, q ≤ 0,10 u "
                f"{int((part['q'] <= Q_PASS).sum())})."
            )
    fomc = tested[tested["predictor"] == "fomc_cut_vs_hold"]
    if len(fomc) and not (fomc["verdict"] == "prošlo").any():
        out.append(
            "FOMC: snížení vs. beze změny nerozlišuje směr ani abnormální pohyb (a překvapení z "
            "FF chybí)."
        )
    out.append("GEX režim: nelze změřit (existuje až od 7/2026, celý v OOS, malé buňky).")
    untested = tests[~tests["tested"].fillna(False).astype(bool) & (tests["section"] == "S1")]
    few = sorted(
        {unit_label(k, u) for k, u in zip(untested["unit_kind"], untested["unit"], strict=True)}
    )
    if few:
        out.append(
            "Nedostatek dat pro podmíněný směr (< 5 v buňce + nebo −, aspoň na jednom horizontu): "
            + ", ".join(few)
            + "."
        )
    exc = vol_tests[
        (vol_tests["predictor"] == "exc_vs_baseline")
        & vol_tests["tested"].fillna(False).astype(bool)
    ]
    quiet = []
    for (kind, unit), rows in exc.groupby(["unit_kind", "unit"]):
        if (rows["verdict"] == "bez efektu").all():
            quiet.append(unit_label(kind, unit))
    if quiet:
        out.append(
            "Výchylka 5–60 min neprokazatelně větší než běžný den (ES ani NQ): "
            + ", ".join(sorted(quiet))
            + "."
        )
    return out


# ── Hlavní běh ─────────────────────────────────────────────────────


def main(args: argparse.Namespace) -> None:
    started = time.monotonic()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)
    frame, history, cut = prepare(args)
    print(
        f"Řádků {len(frame)}, řez IS/OOS {cut:%Y-%m-%d}, historie překvapení {len(history)} releasů"
    )

    tests = verdicts(direction_tests(frame, history, cut))
    sensitivity = overlap_sensitivity(frame, tests)
    sizes, stability = size_tables(frame)
    vol_tests = verdicts(volatility_tests(frame, stability, rng))

    def flat(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        for column in out.columns:
            if out[column].map(lambda v: isinstance(v, tuple)).any():
                out[column] = out[column].map(
                    lambda v: f"{v[0]:.4f}–{v[1]:.4f}" if isinstance(v, tuple) else v
                )
        return out

    flat(tests).to_csv(args.out_dir / "tests_direction.csv", index=False, encoding="utf-8")
    flat(vol_tests).to_csv(args.out_dir / "tests_size.csv", index=False, encoding="utf-8")
    sensitivity.to_csv(args.out_dir / "overlap_sensitivity.csv", index=False, encoding="utf-8")
    sizes.to_csv(args.out_dir / "sizes.csv", index=False, encoding="utf-8")
    stability.to_csv(args.out_dir / "size_stability.csv", index=False, encoding="utf-8")
    posthoc = posthoc_inflation(frame)
    posthoc.drop(columns=["ci_a", "ci_b", "hit_ci", "oos_hit_ci"], errors="ignore").to_csv(
        args.out_dir / "posthoc_inflation.csv", index=False, encoding="utf-8"
    )
    write_report(
        args.out_dir, frame, tests, vol_tests, sensitivity, sizes, stability, cut, history, posthoc
    )

    tested = tests[tests["tested"].fillna(False).astype(bool)]
    print(
        f"Směrových testů {len(tested)}, p<0,05 {int((tested['p'] < 0.05).sum())}, "
        f"q≤0,10 {int((tested['q'] <= Q_PASS).sum())}, "
        f"q_is≤0,10 {int((tested['q_is'] <= Q_PASS).sum())}, prošlo "
        f"{int((tested['verdict'] == 'prošlo').sum())}"
    )
    print(tested["verdict"].value_counts().to_dict())
    print(f"Hotovo za {time.monotonic() - started:.0f} s → {args.out_dir / 'report1296.md'}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=None,
        help="release_reactions.parquet (výchozí <out-dir>/release_reactions.parquet)",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data"
    )
    parser.add_argument(
        "--env-file", type=Path, default=Path(__file__).resolve().parents[1] / ".env"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="Přepočítat cache (bary, historie PG)"
    )
    parser.add_argument("--seed", type=int, default=1296)
    args = parser.parse_args(argv)
    if args.dataset is None:
        args.dataset = args.out_dir / "release_reactions.parquet"
    return args


if __name__ == "__main__":
    main(parse_args())
