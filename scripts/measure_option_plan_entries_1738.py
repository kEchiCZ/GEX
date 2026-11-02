"""Podklad ADR-0062 K3 a K4 (#1738): varianty vstupu A/B a SL plánu QQQ na seanci 2. 11. 2026.

Jen čtení partic, do `data/` ani do DB nic nepíše. QQQ bary v RTH 2. 11. sbírané nebyly
(ad-hoc pohled běžel jen 9:23–9:25 ET), proto se cena QQQ bere z 1m barů NQ vydělených
poměrem NQ/QQQ v minutě vzniku scénáře (14:15 UTC; poměr přes den držel ≤ 0,03 %,
rozbor issuecomment-6585788810). Úrovně NQ jsou z partice `levels` téže minuty, cíle
a vstup z auto-scénáře #29. EM QQQ = straddle ATM 0DTE z ad-hoc snímku 9:23 ET.

Spuštění (z kořene repa, `data` = produkční adresář):
    uv run python scripts/measure_option_plan_entries_1738.py --data data

Ceny putu mezi body = `compute/gexfield.bs_price` (táž funkce jako engine) s IV 30 %
kalibrovanou na skutečné kotace; R:R opce je proto model, R:R podkladu měření.
"""

import argparse
import datetime as dt
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from gexlens_engine.compute.gexfield import bs_price

DAY = "2026-11-02"
CREATED = dt.datetime(2026, 11, 2, 14, 15, tzinfo=dt.UTC)
US_OPEN = dt.datetime(2026, 11, 2, 14, 30, tzinfo=dt.UTC)
SETTLE = dt.datetime(2026, 11, 2, 21, 0, tzinfo=dt.UTC)
#: Auto-scénář #29 (SHORT, skóre −5): vstup a cíle ONL, PDC
SCENARIO_ENTRY, TARGETS = 28753.75, (28587.0, 28529.0)
CET = dt.timezone(dt.timedelta(hours=1))
#: Hlavní varianta 2. 11. (put 687 1DTE, expirace 3. 11. 16:00 ET) a IV kalibrovaná na
#: skutečné kotace (fill 1,40 $ v 16:50 SEČ → 29,8 %, kotace 2,68/2,70 v 18:31 → 30,9 %)
PUT_STRIKE, PUT_IV = 687.0, 0.30
PUT_EXPIRY = dt.datetime(2026, 11, 3, 21, 0, tzinfo=dt.UTC)
#: Čas cíle 1 podle cesty scénáře #29 (16:30 UTC)
PATH_T1 = dt.datetime(2026, 11, 2, 16, 30, tzinfo=dt.UTC)


def rows(path: Path) -> list[dict[str, Any]]:
    return pq.read_table(path).to_pylist()


def hm(ts: dt.datetime) -> str:
    return ts.astimezone(CET).strftime("%H:%M")


def five_minute(bars: Iterable[dict[str, Any]], ratio: float) -> list[dict[str, Any]]:
    """5min bary po hodinách od US openu; razítko = konec baru (close v něm)."""
    out: dict[dt.datetime, dict[str, Any]] = {}
    for bar in bars:
        ts = bar["ts_min"]
        if not (US_OPEN <= ts < SETTLE):
            continue
        start = ts - dt.timedelta(minutes=ts.minute % 5)
        item = out.setdefault(
            start, {"end": start + dt.timedelta(minutes=5), "h": -1e9, "l": 1e9, "c": 0.0}
        )
        item["h"] = max(item["h"], bar["high"] / ratio)
        item["l"] = min(item["l"], bar["low"] / ratio)
        item["c"] = bar["close"] / ratio  # bary jdou časově za sebou
    return [out[key] for key in sorted(out)]


def put_price(spot: float, ts: dt.datetime) -> float:
    tau = (PUT_EXPIRY - ts).total_seconds() / (365 * 86400)
    return bs_price(spot, PUT_STRIKE, PUT_IV, tau, "P")


def option_rr(entry: dict[str, Any], stop: float, tp1: float) -> str:
    """P3 z cen opce: SL hned po vstupu (nejhorší případ), TP1 v čase cíle z cesty scénáře."""
    start = entry["end"]
    price = put_price(entry["c"], start)
    at_stop = put_price(stop, start)
    at_tp1 = put_price(tp1, max(start, PATH_T1))
    return (
        f"put {PUT_STRIKE:g} {price:.2f} $, SL {at_stop:.2f} ({(at_stop - price) * 100:+.0f} $), "
        f"TP1 {at_tp1:.2f} ({(at_tp1 - price) * 100:+.0f} $), R:R opce "
        f"{(at_tp1 - price) / (price - at_stop):.2f}"
    )


def short_entry_a(
    fives: list[dict[str, Any]], level: float, reserve: float, tp1: float, test: str
) -> str:
    """Short A: test úrovně (5min close nad ní, nebo knot), pak první 5min close pod ní."""
    tested, swing, entry = False, 0.0, None
    for bar in fives:
        if entry is None:
            if not tested:
                hit = bar["c"] >= level if test == "close" else bar["h"] >= level
                if hit:
                    tested, swing = True, bar["h"]
                    if test == "wick" and bar["c"] < level:
                        entry = bar
                continue
            swing = max(swing, bar["h"])
            if bar["c"] < level:
                entry = bar
            continue
        stop = swing + reserve
        if bar["c"] > stop:
            return (
                f"vstup {hm(entry['end'])} za {entry['c']:.2f}, SL {stop:.2f} → SL "
                f"{hm(bar['end'])} ({bar['c']:.2f})"
            )
        if bar["l"] <= tp1:
            risk, reward = stop - entry["c"], entry["c"] - tp1
            return (
                f"vstup {hm(entry['end'])} za {entry['c']:.2f}, SL {stop:.2f}, TP1 v 5min baru "
                f"do {hm(bar['end'])}, R:R podkladu {reward / risk:.2f}; "
                + option_rr(entry, stop, tp1)
            )
    return "vstup nespuštěn" if entry is None else f"vstup {hm(entry['end'])}, bez SL i TP1"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    data = parser.parse_args().data
    nq = [r for r in rows(data / f"derived/NQ/bars/{DAY}.parquet") if r["ts_min"]]
    qqq = {r["ts_min"]: r for r in rows(data / f"derived/QQQ/bars/{DAY}.parquet")}
    by_ts = {r["ts_min"]: r for r in nq}
    ratio = by_ts[CREATED]["close"] / qqq[CREATED]["close"]
    print(f"poměr NQ/QQQ {hm(CREATED)} SEČ: {ratio:.4f}")
    level_rows = rows(data / f"derived/NQ/20261102/levels/{DAY}.parquet")
    levels = next(r for r in level_rows if r["ts_min"] == CREATED)
    overnight = [r for r in nq if r["ts_min"] < US_OPEN]
    onh = max(r["high"] for r in overnight)
    entry_q = SCENARIO_ENTRY / ratio
    tp1, tp2 = (target / ratio for target in TARGETS)
    print(
        f"QQQ: vstup {entry_q:.2f}, flip {levels['flip'] / ratio:.2f}, ONH {onh / ratio:.2f} "
        f"(bary od 0:00 UTC), těžiště {levels['centroid'] / ratio:.2f}, call wall "
        f"{levels['call_wall'] / ratio:.2f}, TP1 {tp1:.2f}, TP2 {tp2:.2f}"
    )
    chain = rows(data / "snapshots/QQQ/20261102/2026-11-02.parquet")
    first = min(r["ts_min"] for r in chain)
    snap = {(r["strike"], r["right"]): r for r in chain if r["ts_min"] == first}
    spot = qqq[first]["close"] if first in qqq else entry_q
    atm = min({k for k, _ in snap}, key=lambda k: abs(k - spot))
    em = sum((snap[(atm, side)]["bid"] + snap[(atm, side)]["ask"]) / 2 for side in "CP")
    gex: dict[float, float] = {}
    for (strike, side), r in snap.items():
        if r["gamma"] and r["oi"]:
            sign = 1.0 if side == "C" else -1.0
            gex[strike] = gex.get(strike, 0.0) + sign * r["gamma"] * r["oi"] * 100 * spot**2 * 0.01
    top = max(gex, key=lambda k: gex[k])
    print(
        f"EM QQQ (straddle {atm:g}, {hm(first)} SEČ) {em:.2f}; největší kladná GEX {top:g} "
        f"({gex[top] / 1e6:+.0f} M), {(top - spot) / em:.2f} EM od ceny {spot:.2f}"
    )
    reserve = 0.05 * em
    fives = five_minute(nq, ratio)
    print(f"\nShort A, rezerva 0,05 EM = {reserve:.2f}, test = 5min close za úrovní:")
    for name, level in (
        ("flip", levels["flip"] / ratio),
        ("ONH", onh / ratio),
        ("těžiště", levels["centroid"] / ratio),
        ("697,0 (ruční plán)", 697.0),
    ):
        print(f"  {name} {level:.2f}: {short_entry_a(fives, level, reserve, tp1, 'close')}")
    print("Short A, test knotem:")
    for name, level in (("flip", levels["flip"] / ratio), ("697,0 (ruční plán)", 697.0)):
        print(f"  {name} {level:.2f}: {short_entry_a(fives, level, reserve, tp1, 'wick')}")
    print("Rezerva SL u úrovně flip (test close):")
    for label, value in (("0", 0.0), ("0,05 EM", reserve), ("0,1 EM", 0.1 * em)):
        print(f"  {label}: {short_entry_a(fives, levels['flip'] / ratio, value, tp1, 'close')}")
    b_level = entry_q - reserve
    b_bar = next(bar for bar in fives if bar["c"] < b_level)
    sl_b = onh / ratio + reserve
    stop_bar = next((bar for bar in fives if bar["end"] > b_bar["end"] and bar["c"] > sl_b), None)
    high = max(bar["h"] for bar in fives)
    print(
        f"\nShort B: první 5min close < {b_level:.2f} v {hm(b_bar['end'])} za {b_bar['c']:.2f}; "
        f"SL B {sl_b:.2f} → "
        + (f"SL {hm(stop_bar['end'])} ({stop_bar['c']:.2f})" if stop_bar else "nezasažen")
        + f"; high dne {high:.2f} = {(high - sl_b) / em:.2f} EM za SL B"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
