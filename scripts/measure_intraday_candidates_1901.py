"""Kandidáti intradenního setupu pro MNQ/MES se známkou A/B/C (#1901) — jen čtení.

Rozbor: `docs/research/1901-kandidati-mnq-mes.md`. Pravidla, faktory známek a předpovědi jsou
předregistrované 26. 11. 2026 nad backtestem 8/2024 → 25. 11. 2026. **Od 27. 11. 2026 se skript
nemění:** dopředné vyhodnocení #1901 je týž kód s `--from 2026-11-27`. Jiná pravidla = nový
skript a nová předregistrace, ne úprava tohoto.

Data: RTH minutové bary `data/derived/{NQ,ES}/bars/` (9:30–16:00 ET, 390 barů; bar k = minuta
9:30 + k, cena v čase 9:31 + k = close baru k). Jen obchodní dny bez zkrácených seancí
(`settle.is_trading_session`, `settle.is_early_close`); seance s < 380 bary se vynechá a přeruší
okno σ strategie S1. Náklad round-trip 1 mikra = `setup_summary.round_trip_cost_usd` v konvenci
v6 (poplatek mikra `SetupParams.fee_per_micro_contract_usd` + 1 tick na stranu): MNQ 1,11 b,
MES 0,744 b. Výsledek obchodu v čistém R = (hrubé body − náklad) / R.

Strategie (parametry z pramenů nebo konvence, neladí se):
  S1 momentum z pásma šumu (Zarattini, Aziz, Barbon 2024, „Beat the Market"): σ(t) = Ø přes
     14 předchozích seancí |cena(t) / open − 1|; UB = max(open, close předchozí seance) × (1 + σ),
     LB = min(…) × (1 − σ); rozhodnutí jen v HH:00 a HH:30 od 10:00 do 15:30 ET; long nad UB,
     short pod LB; pohyblivý stop long max(UB, VWAP), short min(LB, VWAP), kontrola jen
     v rozhodovacích časech; navíc tvrdý stop R = 50 b NQ / 20 b ES od vstupu, kontrolovaný
     každou minutou (high/low); vše zavřeno v 16:00 ET.
  S2 ORB 5 min (Zarattini, Barbon, Aziz 2024): směr první 5min svíčky (doji = bez obchodu),
     vstup stop příkazem na její high/low, stop R = 10 % ATR(14) z RTH předchozích seancí, výstup
     16:00 ET. Bar vstupu, který zasáhne i stop, se počítá jako stop (konzervativně).
  S3 fade falešného průrazu high/low předchozí seance („turtle soup"): průraz do 15:00 ET → do
     30 min close zpět za úroveň → vstup proti průrazu na close, stop = extrém od průrazu ± 1 tick,
     cíl 2 R, jinak 16:00 ET; gap přes úroveň není průraz; nejvýš 1 obchod na stranu.

Známky (směr předpovědi předem, A > B > C):
  G1 narrow = předchozí seance NR4 nebo NR7 (rozsah RTH nejmenší za 4 / 7 seancí);
     in_play = RVOL5 ≥ 1 (objem 9:30–9:35 / Ø týchž minut 14 předchozích seancí, jen když celé
     okno má týž zdroj barů jako den — archiv a živé bary mají jinou škálu objemu).
     Trend (S1, S2): A = oba, B = jeden, C = žádný. Fade (S3): A = žádný, B = jeden, C = oba.
  G2 hi_vol = ATR(14) / close předchozí seance nad mediánem téže veličiny za 60 předchozích
     seancí (≥ 40 hodnot); catalyst = High-impact USD událost kalendáře se začátkem před 16:00 ET.
     A = oba, B = jeden, C = žádný. G2 vzniklo po selhání G1 → čte se s vědomím
     mnohonásobného testování.
  Gamma režim (`setups.gex_regime` z levels 0DTE v minutě vstupu, data od 20. 7. 2026) jen popisně.
Účet 5 000 $ (ADR-0038): kontrakty = ⌊riziko $ / (R b × $ za bod mikra)⌋, 0 = obchod se nevezme;
  „1 %" = 50 $, „2 %" = 100 $, „A/B/C" podle G2 = A 100 $, B 50 $, C 0.
Testy: 6 primárních (strategie × symbol, Ø čisté R > 0), Bonferroni α = 0,05 / 6 → t ≥ 2,4.
Dopředné vyhodnocení (#1901): kolaps = horní mez oboustranného 95% intervalu Ø čistého R < 0;
  brána aplikace = `risk.template_gate` (n ≥ 30, jednostranná 95% dolní mez > 0); potřebný vzorek
  pro edge +0,2 R = `risk.samples_needed`.

Kalendář (CSV `ts_event,name`; jen SELECT nad `news_events`):
  docker exec gex-postgres-1 psql -U gexlens -d gexlens -c "COPY (SELECT ts_event,
    coalesce(raw->>'name', raw->>'title') AS name FROM news_events WHERE source = 'forexfactory'
    AND coalesce(raw->>'country', raw->>'currency') = 'USD'
    AND lower(coalesce(raw->>'impact', raw->>'impactName')) = 'high' ORDER BY ts_event)
    TO STDOUT WITH CSV HEADER" > usd_high.csv
Spuštění z kořene repa (data produkce jen čte):
  uv run python scripts/measure_intraday_candidates_1901.py --calendar usd_high.csv
  uv run python scripts/measure_intraday_candidates_1901.py --calendar usd_high.csv \
      --from 2026-11-27 --symbols NQ
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from gexlens_engine.compute.risk import expectancy_lower_bound, samples_needed, template_gate
from gexlens_engine.compute.settle import (
    is_early_close,
    is_trading_session,
    settle_ts,
    us_open_ts,
)
from gexlens_engine.compute.setup_summary import MICRO_SYMBOLS, round_trip_cost_usd
from gexlens_engine.compute.setups import SetupParams, gex_regime
from gexlens_engine.ticker import FUTURES_MULTIPLIERS

DATA = Path("data/derived")
ET = ZoneInfo("America/New_York")
TICK = 0.25
LOOKBACK = 14
S1_HARD = {"NQ": 50.0, "ES": 20.0}
ACCOUNT_USD = 5000.0
RISK_BY_GRADE = {"A": 100.0, "B": 50.0, "C": 0.0}
GATE_MIN_SAMPLES = SetupParams().template_gate_min_samples
DECISIONS = [*range(29, 389, 30), 389]  # ceny v 10:00, 10:30 … 15:30 a 16:00 ET

Day = dict[str, Any]
Trade = dict[str, Any]


def point_usd(sym: str) -> float:
    return FUTURES_MULTIPLIERS[MICRO_SYMBOLS[sym]]


def cost_points(sym: str) -> float:
    """Round-trip náklad 1 mikra v bodech (konvence v6)."""
    micro = MICRO_SYMBOLS[sym]
    usd = round_trip_cost_usd(
        micro,
        point_usd(sym),
        per_side=True,
        micro_fee_usd=SetupParams().fee_per_micro_contract_usd,
    )
    if usd is None:
        raise ValueError(f"Náklad {micro} nejde spočítat")
    return usd / point_usd(sym)


# ── data ─────────────────────────────────────────────────────────────────────
def build_days(sym: str) -> list[Day]:
    files = sorted((DATA / sym / "bars").glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"Žádné bary v {DATA / sym / 'bars'} (spouštět z kořene repa)")
    df = pd.concat(pd.read_parquet(p) for p in files)
    df = df.drop_duplicates("ts_min").set_index("ts_min").sort_index()
    if "source" not in df:
        df["source"] = None
    first, last = df.index.min().date(), df.index.max().date()
    out: list[Day] = []
    for i in range((last - first).days + 1):
        d = first + dt.timedelta(days=i)
        if not is_trading_session(d) or is_early_close(d):
            continue
        start, end = pd.Timestamp(us_open_ts(d)), pd.Timestamp(settle_ts(d))
        bars = df.loc[start : end - pd.Timedelta(minutes=1)]
        if len(bars) < 380:
            continue
        idx = pd.date_range(start, end - pd.Timedelta(minutes=1), freq="1min")
        b = bars.reindex(idx)
        ohlc = ["open", "high", "low", "close"]
        b[ohlc] = b[ohlc].ffill()
        out.append(
            {
                "day": d,
                "o": b["open"].to_numpy(),
                "h": b["high"].to_numpy(),
                "l": b["low"].to_numpy(),
                "c": b["close"].to_numpy(),
                "v": b["volume"].fillna(0).to_numpy(),
                "live": bool(bars["source"].notna().any()),
            }
        )
    return out


def load_catalyst_days(path: Path) -> set[dt.date]:
    cal = pd.read_csv(path, parse_dates=["ts_event"])
    if cal.empty:
        raise ValueError(f"Kalendář {path} je prázdný")
    days = set()
    for ts in cal["ts_event"]:
        local = pd.Timestamp(ts).tz_convert(ET)
        if local.hour < 16:
            days.add(local.date())
    return days


def add_features(days: list[Day], catalyst_days: set[dt.date]) -> None:
    for x in days:
        x["H"], x["L"], x["C"] = x["h"].max(), x["l"].min(), x["c"][-1]
        x["vol5"] = x["v"][:5].sum()
    for i, x in enumerate(days):
        x["prev"] = days[i - 1] if i > 0 else None
        x["atr"] = x["narrow"] = x["rvol"] = x["atrp"] = None
        if i >= 15:
            trs = []
            for j in range(i - 14, i):
                p, q = days[j - 1], days[j]
                trs.append(max(q["H"] - q["L"], abs(q["H"] - p["C"]), abs(q["L"] - p["C"])))
            x["atr"] = float(np.mean(trs))
            x["atrp"] = x["atr"] / days[i - 1]["C"]
            ranges = [days[j]["H"] - days[j]["L"] for j in range(i - 7, i)]
            x["narrow"] = ranges[-1] <= min(ranges[-4:]) or ranges[-1] <= min(ranges)
            window = days[i - 14 : i]
            if all(w["live"] == x["live"] for w in window):
                base = np.mean([w["vol5"] for w in window])
                x["rvol"] = x["vol5"] / base if base > 0 else None
        hist = [w["atrp"] for w in days[max(0, i - 60) : i] if w["atrp"] is not None]
        x["hi_vol"] = (
            None if len(hist) < 40 or x["atrp"] is None else x["atrp"] > float(np.median(hist))
        )
        x["catalyst"] = x["day"] in catalyst_days


def grade_g1(x: Day, kind: str) -> str | None:
    if x["narrow"] is None or x["rvol"] is None:
        return None
    k = int(bool(x["narrow"])) + int(x["rvol"] >= 1.0)
    order = "ABC" if kind == "trend" else "CBA"
    return order[2 - k]


def grade_g2(x: Day) -> str | None:
    if x["hi_vol"] is None:
        return None
    return "ABC"[2 - (int(x["hi_vol"]) + int(x["catalyst"]))]


# ── strategie: obchod = {day, dir, k, entry, R, gross} ──────────────────────
def s1(sym: str, days: list[Day]) -> list[Trade]:
    trades: list[Trade] = []
    moves: list[np.ndarray] = []
    hard = S1_HARD[sym]
    for x in days:
        o0, c, hi, lo = x["o"][0], x["c"], x["h"], x["l"]
        prev = x["prev"]
        contiguous = prev is not None and (x["day"] - prev["day"]).days <= 4
        if len(moves) >= LOOKBACK and contiguous:
            sigma = np.mean(moves[-LOOKBACK:], axis=0)
            ub = max(o0, prev["C"]) * (1 + sigma)
            lb = min(o0, prev["C"]) * (1 - sigma)
            tp = (hi + lo + c) / 3
            cv = np.cumsum(x["v"])
            vwap = np.where(cv > 0, np.cumsum(tp * x["v"]) / np.maximum(cv, 1e-9), c)
            pos, entry, checked, ek = 0, 0.0, 0, 0
            for k in DECISIONS:
                exit_px = None
                if pos != 0:
                    stop_px = entry - pos * hard
                    span = range(checked + 1, k + 1)
                    if any((lo[j] <= stop_px) if pos == 1 else (hi[j] >= stop_px) for j in span):
                        exit_px = stop_px
                    checked = k
                if exit_px is None and pos != 0:
                    trail = max(ub[k], vwap[k]) if pos == 1 else min(lb[k], vwap[k])
                    if k == 389 or pos * (c[k] - trail) < 0:  # 16:00 nebo pod pohyblivým stopem
                        exit_px = c[k]
                if exit_px is not None:
                    trades.append(
                        {
                            "day": x["day"],
                            "dir": pos,
                            "k": ek,
                            "entry": entry,
                            "R": hard,
                            "gross": pos * (exit_px - entry),
                        }
                    )
                    pos = 0
                if k == 389:
                    break
                if pos == 0:
                    if c[k] > ub[k]:
                        pos, entry, checked, ek = 1, c[k], k, k
                    elif c[k] < lb[k]:
                        pos, entry, checked, ek = -1, c[k], k, k
        if not contiguous:
            moves.clear()
        moves.append(np.abs(c / o0 - 1))
    return trades


def s2(sym: str, days: list[Day]) -> list[Trade]:
    trades: list[Trade] = []
    for x in days:
        if x["atr"] is None:
            continue
        o, c, hi, lo = x["o"], x["c"], x["h"], x["l"]
        if c[4] == o[0]:
            continue
        d = 1 if c[4] > o[0] else -1
        level = hi[:5].max() if d == 1 else lo[:5].min()
        risk = 0.1 * x["atr"]
        for k in range(5, 390):
            if (d == 1 and hi[k] >= level) or (d == -1 and lo[k] <= level):
                entry = max(level, o[k]) if d == 1 else min(level, o[k])
                stop_px = entry - d * risk
                exit_px = c[389]
                for j in range(k, 390):
                    if (d == 1 and lo[j] <= stop_px) or (d == -1 and hi[j] >= stop_px):
                        exit_px = stop_px
                        break
                trades.append(
                    {
                        "day": x["day"],
                        "dir": d,
                        "k": k,
                        "entry": entry,
                        "R": risk,
                        "gross": d * (exit_px - entry),
                    }
                )
                break
    return trades


def s3(sym: str, days: list[Day]) -> list[Trade]:
    trades: list[Trade] = []
    for x in days:
        prev = x["prev"]
        if prev is None or x["atr"] is None:
            continue
        o, c, hi, lo = x["o"], x["c"], x["h"], x["l"]
        for side, level in ((1, prev["H"]), (-1, prev["L"])):  # side 1 = průraz nahoru → short
            if (side == 1 and o[0] > level) or (side == -1 and o[0] < level):
                continue  # gap přes úroveň = není průraz v seanci
            brk = next(
                (k for k in range(330) if (hi[k] > level if side == 1 else lo[k] < level)), None
            )
            if brk is None:
                continue
            ext = hi[brk] if side == 1 else lo[brk]
            for k in range(brk, min(brk + 31, 330)):
                ext = max(ext, hi[k]) if side == 1 else min(ext, lo[k])
                back = c[k] < level if side == 1 else c[k] > level
                if k == brk or not back:
                    continue
                d = -side
                entry = c[k]
                stop_px = ext + TICK if side == 1 else ext - TICK
                risk = abs(stop_px - entry)
                target = entry + d * 2 * risk
                exit_px = c[389]
                for j in range(k + 1, 390):
                    if (d == -1 and hi[j] >= stop_px) or (d == 1 and lo[j] <= stop_px):
                        exit_px = stop_px
                        break
                    if (d == -1 and lo[j] <= target) or (d == 1 and hi[j] >= target):
                        exit_px = target
                        break
                trades.append(
                    {
                        "day": x["day"],
                        "dir": d,
                        "k": k,
                        "entry": entry,
                        "R": risk,
                        "gross": d * (exit_px - entry),
                    }
                )
                break
    return trades


# ── vyhodnocení ──────────────────────────────────────────────────────────────
def regime_at(sym: str, d: dt.date, k: int, price: float) -> str | None:
    path = DATA / sym / d.strftime("%Y%m%d") / "levels" / f"{d.isoformat()}.parquet"
    if not path.exists():
        return None
    ts = pd.Timestamp(us_open_ts(d)) + pd.Timedelta(minutes=k)
    lv = pd.read_parquet(path).set_index("ts_min").sort_index().loc[:ts]
    if lv.empty or (ts - lv.index[-1]) > pd.Timedelta(minutes=5):
        return None
    row = lv.iloc[-1]
    flip = None if pd.isna(row["flip"]) else float(row["flip"])
    total = 0.0 if pd.isna(row["total_gex"]) else float(row["total_gex"])
    return gex_regime(price, flip, total)


def stat(r: pd.Series) -> str:
    n = len(r)
    if n < 2:
        return f"n={n:>3}"
    m, sd = r.mean(), r.std(ddof=1)
    return f"n={n:>3} hit={(r > 0).mean():4.0%} ØR={m:+.3f} t={m / (sd / math.sqrt(n)):+5.2f}"


def verdict(r: pd.Series) -> str:
    """Dopředná kritéria #1901: kolaps, brána aplikace, potřebný vzorek pro +0,2 R."""
    values = [float(v) for v in r]
    upper = expectancy_lower_bound(values, z=-1.96)
    gate = template_gate(values, min_samples=GATE_MIN_SAMPLES, enabled=True)
    needed = samples_needed(values, min_samples=GATE_MIN_SAMPLES)
    collapse = "?" if upper is None else ("ANO" if upper < 0 else "ne")
    lb = "—" if gate.lower_bound is None else f"{gate.lower_bound:+.3f}"
    up = "—" if upper is None else f"{upper:+.3f}"
    return (
        f"kolaps {collapse} (horní mez 95 % {up}) · brána {gate.verdict} (n={gate.n}, LB {lb})"
        f" · potřeba n≈{needed}"
    )


def account(tr: pd.DataFrame, sym: str, sizing: str) -> str:
    cost, usd = cost_points(sym), point_usd(sym)
    eq = peak = mdd = 0.0
    taken = 0
    for t in tr.sort_values(["day", "k"]).itertuples():
        if sizing == "abc":
            risk = RISK_BY_GRADE.get(t.grade2, 0.0) if isinstance(t.grade2, str) else 0.0
        else:
            risk = {"1 %": 50.0, "2 %": 100.0}[sizing]
        n = int(risk // (t.R * usd)) if t.R > 0 else 0
        if n < 1:
            continue
        taken += 1
        eq += n * (t.gross - cost) * usd
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return (
        f"obchodů {taken:>3}  Σ {eq:+7.0f} $  maxDD {mdd:+6.0f} $ ({mdd / ACCOUNT_USD:+.0%} účtu)"
    )


STRATS: dict[str, tuple[Callable[[str, list[Day]], list[Trade]], str]] = {
    "S1 momentum z pásma šumu": (s1, "trend"),
    "S2 ORB 5 min": (s2, "trend"),
    "S3 fade high/low předchozí seance": (s3, "fade"),
}


def report(sym: str, days: list[Day], start: dt.date | None, end: dt.date | None) -> None:
    feat = {x["day"]: x for x in days}
    cost = cost_points(sym)
    sessions = [x["day"] for x in days if (start is None or x["day"] >= start)]
    sessions = [d for d in sessions if end is None or d <= end]
    span = f"{sessions[0]} – {sessions[-1]}" if sessions else "žádná seance"
    print(f"\n################ {sym}  {span}  ({len(sessions)} seancí, náklad {cost:.3f} b)")
    for name, (fn, kind) in STRATS.items():
        tr = pd.DataFrame(fn(sym, days))
        if not tr.empty:
            if start is not None:
                tr = tr[tr["day"] >= start]
            if end is not None:
                tr = tr[tr["day"] <= end]
        print(f"\n== {name}")
        if tr.empty:
            print("   žádný obchod")
            continue
        tr = tr.copy()
        tr["netR"] = (tr["gross"] - cost) / tr["R"]
        tr["grade1"] = [grade_g1(feat[d], kind) for d in tr["day"]]
        tr["grade2"] = [grade_g2(feat[d]) for d in tr["day"]]
        print(f"   medián R {tr['R'].median():.1f} b")
        print("   vše          :", stat(tr["netR"]))
        print("   dopředně     :", verdict(tr["netR"]))
        halves = tr["day"].map(lambda d: f"{d.year}H{1 if d.month <= 6 else 2}")
        for half, g in tr.groupby(halves):
            print(f"     {half}     :", stat(g["netR"]))
        for col, label in (("grade1", "G1"), ("grade2", "G2")):
            for gr in "ABC":
                print(f"   {label} {gr}         :", stat(tr[tr[col] == gr]["netR"]))
            print(f"   {label} bez      :", stat(tr[tr[col].isna()]["netR"]))
        recent = tr[tr["day"] >= dt.date(2026, 7, 20)]
        regimes = [regime_at(sym, t.day, t.k, t.entry) for t in recent.itertuples()]
        for reg in ("negative", "positive"):
            mask = [r == reg for r in regimes]
            print(f"   gamma {reg:<8}:", stat(recent[mask]["netR"]))
        print("   účet 1 %     :", account(tr, sym, "1 %"))
        print("   účet 2 %     :", account(tr, sym, "2 %"))
        print("   účet A/B/C G2:", account(tr, sym, "abc"))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--calendar", type=Path, required=True, help="CSV ts_event,name (High USD)")
    ap.add_argument("--from", dest="start", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--to", dest="end", type=dt.date.fromisoformat, default=None)
    ap.add_argument("--symbols", nargs="+", default=["NQ", "ES"], choices=sorted(S1_HARD))
    args = ap.parse_args()
    catalyst_days = load_catalyst_days(args.calendar)
    for sym in args.symbols:
        days = build_days(sym)
        add_features(days, catalyst_days)
        report(sym, days, args.start, args.end)


if __name__ == "__main__":
    main()
