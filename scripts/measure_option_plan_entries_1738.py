"""Podklad ADR-0062 K3 a K4 (#1738): varianty vstupu A/B a SL plánu QQQ na seanci 2. 11. 2026.

Jen čtení partic, do `data/` ani do DB nic nepíše. QQQ bary v RTH 2. 11. sbírané nebyly
(ad-hoc pohled běžel jen 9:23–9:25 ET), proto se cena QQQ bere z 1m barů NQ vydělených
poměrem NQ/QQQ poslední uzavřené minuty před vznikem scénáře (bar 14:14 UTC; bar 14:15
končí až 59 s po vzniku). Poměr přes den držel do 0,03 % (rozbor issuecomment-6585788810).
Seance začíná 17:00 CT předchozího dne, takže ONH/ONL se čtou i z partice D−1 (#1002).
Úrovně NQ jsou ze snímku `levels` minuty vzniku (stav ve 14:15:00), cíle a vstup
z auto-scénáře #29. EM QQQ = `compute/emrespect.straddle_em` nad ad-hoc snímkem 0DTE 9:23 ET.

Ceny putu = `compute/gexfield.bs_price` (táž funkce jako engine) s jednou IV 30 % — R:R
opce je model, R:R podkladu měření. P3: SL i TP1 oceněné v témže čase (cíl 1 podle cesty
scénáře, nejdřív v čase vstupu); přísná varianta oceňuje SL v čase exitu (settle − 15 min,
put s časem ztrácí). P4: ztráta na úrovni SL + 0,2 EM v čase exitu.

Spuštění (z kořene repa, `data` = produkční adresář):
    uv run python scripts/measure_option_plan_entries_1738.py --data data
"""

import argparse
import datetime as dt
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from gexlens_engine.compute.emrespect import StraddleQuote, straddle_em
from gexlens_engine.compute.gexfield import bs_price
from gexlens_engine.compute.settle import settle_ts

DAY = dt.date(2026, 11, 2)
CREATED = dt.datetime(2026, 11, 2, 14, 15, tzinfo=dt.UTC)
SESSION_START = dt.datetime(2026, 11, 1, 23, 0, tzinfo=dt.UTC)  # 17:00 CT
US_OPEN = dt.datetime(2026, 11, 2, 14, 30, tzinfo=dt.UTC)
SETTLE = settle_ts(DAY)
EXIT = SETTLE - dt.timedelta(minutes=15)
#: Auto-scénář #29 (SHORT, skóre −5): vstup a cíle ONL, PDC; cíl 1 cesty v 16:30 UTC
SCENARIO_ENTRY, TARGETS = 28753.75, (28587.0, 28529.0)
PATH_T1 = dt.datetime(2026, 11, 2, 16, 30, tzinfo=dt.UTC)
#: Hlavní varianta 2. 11.: put 687 1DTE (expirace 3. 11. 16:00 ET)
PUT_STRIKE, PUT_IV = 687.0, 0.30
PUT_EXPIRY = settle_ts(dt.date(2026, 11, 3))
CET = dt.timezone(dt.timedelta(hours=1))


def rows(path: Path) -> list[dict[str, Any]]:
    return pq.read_table(path).to_pylist()


def hm(ts: dt.datetime) -> str:
    return ts.astimezone(CET).strftime("%H:%M")


def put_price(spot: float, ts: dt.datetime) -> float:
    tau = (PUT_EXPIRY - ts).total_seconds() / (365 * 86400)
    return bs_price(spot, PUT_STRIKE, PUT_IV, tau, "P")


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


def option_line(entry: dict[str, Any], stop: float, tp1: float, em: float) -> str:
    start = entry["end"]
    horizon = max(start, PATH_T1)
    price = put_price(entry["c"], start)
    at_stop, at_tp1 = put_price(stop, horizon), put_price(tp1, horizon)
    strict = put_price(stop, EXIT)
    worst = put_price(stop + 0.2 * em, EXIT)
    return (
        f"put {price:.2f} $, SL {at_stop:.2f} ({(at_stop - price) * 100:+.0f} $), "
        f"TP1 {at_tp1:.2f} ({(at_tp1 - price) * 100:+.0f} $), R:R opce "
        f"{(at_tp1 - price) / (price - at_stop):.2f} (SL v exitu "
        f"{(at_tp1 - price) / (price - strict):.2f}); P4 SL + 0,2 EM v exitu "
        f"{(worst - price) * 100:+.0f} $"
    )


def short_entry_a(
    fives: list[dict[str, Any]], level: float, reserve: float, tp1: float, test: str, em: float
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
                + option_line(entry, stop, tp1, em)
            )
    return "vstup nespuštěn" if entry is None else f"vstup {hm(entry['end'])}, bez SL i TP1"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    data = parser.parse_args().data
    nq = sorted(
        (
            r
            for day in ("2026-11-01", "2026-11-02")
            for r in rows(data / f"derived/NQ/bars/{day}.parquet")
            if r["ts_min"] and r["ts_min"] >= SESSION_START
        ),
        key=lambda r: r["ts_min"],
    )
    qqq = {r["ts_min"]: r for r in rows(data / "derived/QQQ/bars/2026-11-02.parquet")}
    by_ts = {r["ts_min"]: r for r in nq}
    closed = CREATED - dt.timedelta(minutes=1)
    ratio = by_ts[closed]["close"] / qqq[closed]["close"]
    print(f"poměr NQ/QQQ baru {hm(closed)} SEČ (poslední uzavřený před vznikem): {ratio:.4f}")
    level_rows = rows(data / "derived/NQ/20261102/levels/2026-11-02.parquet")
    levels = next(r for r in level_rows if r["ts_min"] == CREATED)
    overnight = [r for r in nq if r["ts_min"] < US_OPEN]
    onh, onl = max(r["high"] for r in overnight), min(r["low"] for r in overnight)
    entry_q = SCENARIO_ENTRY / ratio
    tp1, tp2 = (target / ratio for target in TARGETS)
    print(
        f"QQQ: vstup {entry_q:.2f}, flip {levels['flip'] / ratio:.2f}, ONH {onh / ratio:.2f}, "
        f"ONL {onl / ratio:.2f} (NQ {onl:g}), těžiště {levels['centroid'] / ratio:.2f}, call wall "
        f"{levels['call_wall'] / ratio:.2f}, TP1 {tp1:.2f}, TP2 {tp2:.2f}"
    )
    chain = rows(data / "snapshots/QQQ/20261102/2026-11-02.parquet")
    first = min(r["ts_min"] for r in chain)
    snap = {(r["strike"], r["right"]): r for r in chain if r["ts_min"] == first}
    spot = qqq[first]["close"]
    mids = {key: (r["bid"] + r["ask"]) / 2 for key, r in snap.items() if r["bid"] and r["ask"]}
    quotes = [
        StraddleQuote(strike=k, call_mid=mids.get((k, "C"), 0.0), put_mid=mids.get((k, "P"), 0.0))
        for k in sorted({k for k, _ in snap})
    ]
    em_result = straddle_em(quotes, spot)
    if em_result is None:
        print("EM: straddle bez kotací")
        return 1
    atm, em = em_result
    gex: dict[float, float] = {}
    for (strike, side), r in snap.items():
        if r["gamma"] and r["oi"]:
            sign = 1.0 if side == "C" else -1.0
            gex[strike] = gex.get(strike, 0.0) + sign * r["gamma"] * r["oi"] * 100 * spot**2 * 0.01
    top = max(gex, key=lambda k: gex[k])
    print(
        f"EM QQQ (straddle {atm:g}, {hm(first)} SEČ) {em:.2f}; největší kladná GEX {top:g} "
        f"({gex[top] / 1e6:+.0f} M), {(top - spot) / em:.2f} EM od spotu {spot:.2f}"
    )
    fill = put_price(696.67, dt.datetime(2026, 11, 2, 15, 50, tzinfo=dt.UTC))
    quote = put_price(691.9, dt.datetime(2026, 11, 2, 17, 31, tzinfo=dt.UTC))
    print(
        f"model putu 687 s IV 30 %: fill 16:50 SEČ {fill:.2f} (skutečně 1,40), "
        f"18:31 SEČ {quote:.2f} (kotace 2,68/2,70)"
    )
    reserve = 0.05 * em
    fives = five_minute(nq, ratio)
    flip = levels["flip"] / ratio
    print(f"\nShort A, rezerva 0,05 EM = {reserve:.2f}, test = 5min close za úrovní:")
    for name, level in (
        ("flip", flip),
        ("ONH", onh / ratio),
        ("těžiště", levels["centroid"] / ratio),
        ("697,0 (ruční plán)", 697.0),
    ):
        print(f"  {name} {level:.2f}: {short_entry_a(fives, level, reserve, tp1, 'close', em)}")
    print("Short A, test knotem:")
    for name, level in (("flip", flip), ("697,0 (ruční plán)", 697.0)):
        print(f"  {name} {level:.2f}: {short_entry_a(fives, level, reserve, tp1, 'wick', em)}")
    print("Rezerva SL u úrovně flip (test close):")
    for label, value in (("0", 0.0), ("0,05 EM", reserve), ("0,1 EM", 0.1 * em)):
        print(f"  {label}: {short_entry_a(fives, flip, value, tp1, 'close', em)}")
    b_level = entry_q - reserve
    b_bar = next(bar for bar in fives if bar["c"] < b_level)
    print(f"\nShort B: první 5min close < {b_level:.2f} v {hm(b_bar['end'])} za {b_bar['c']:.2f}")
    for name, level in (("úroveň A = flip", flip), ("ONH (rozbor)", onh / ratio)):
        sl_b = level + reserve
        hit = next((bar for bar in fives if bar["end"] > b_bar["end"] and bar["c"] > sl_b), None)
        if hit is None:
            print(f"  SL B za {name} {sl_b:.2f}: nezasažen")
            continue
        print(
            f"  SL B za {name} {sl_b:.2f} → {hm(hit['end'])} (close {hit['c']:.2f}, high "
            f"{hit['h']:.2f} = {(hit['h'] - sl_b) / em:.2f} EM za SL, close "
            f"{(hit['c'] - sl_b) / em:.2f} EM)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
