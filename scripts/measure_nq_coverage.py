"""Meření pokrytí NQ metodikou #609 — akceptační kritérium fáze #616 4d.

Ze snapshot partic: podíl striků se `stale_age` > 60 s po hodinách (churn
IBKR rotace pod stropem 100 lines) + objem `greekssource` (BS fallback #547,
známka že TWS model nedodává). Před #616 mělo NQ přes US seanci 12–16 %
stale a ~25 striků/min BS fallback; po přesunu šířky na dxFeed má degradace
zmizet — tenhle skript dává srovnatelná čísla před/po.

Spuštění (v engine kontejneru, jen čte parquet):
    python measure_nq_coverage.py --symbol NQ --dates 2026-08-19,2026-08-20
"""

import argparse
import glob
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

DATA = Path("data")
STALE_S = 60.0
US_HOURS_UTC = range(13, 21)  # 13:00–20:59 UTC ≈ US seance


def measure(symbol: str, date: str) -> None:
    snap_files = sorted(glob.glob(str(DATA / "snapshots" / symbol / "*" / f"{date}.parquet")))
    if not snap_files:
        print(f"{symbol} {date}: žádné snapshoty")
        return
    frames = []
    for file in snap_files:
        expiry = Path(file).parent.name
        frame = pd.read_parquet(file, columns=["ts_min", "stale_age"])
        frame["expiry"] = expiry
        frames.append(frame)
    snaps = pd.concat(frames)
    snaps["hour"] = pd.to_datetime(snaps["ts_min"]).dt.hour
    per_hour = (
        snaps.assign(stale=snaps["stale_age"] > STALE_S)
        .groupby("hour")
        .agg(rows=("stale", "size"), stale_share=("stale", "mean"))
    )
    us = per_hour[per_hour.index.isin(US_HOURS_UTC)]
    print(f"\n=== {symbol} {date} — podíl striků stale>{STALE_S:.0f}s po hodinách UTC ===")
    for hour, row in per_hour.iterrows():
        marker = " <- US" if hour in US_HOURS_UTC else ""
        print(f"  {hour:02d}h  {row.stale_share:6.1%}  (n={int(row.rows)}){marker}")
    if len(us):
        print(f"  US seance průměr: {us.stale_share.mean():.1%} (před #616: 12–16 %)")

    # BS fallback objem (greekssource) per expirace
    gs_files = sorted(
        glob.glob(str(DATA / "derived" / symbol / "*" / "greekssource" / f"{date}.parquet"))
    )
    total = 0
    for file in gs_files:
        expiry = Path(file).parents[1].name
        count = len(pd.read_parquet(file, columns=["ts_min"]))
        total += count
        print(f"  greekssource {expiry}: {count} řádků")
    minutes = snaps["ts_min"].nunique() or 1
    print(f"  BS fallback celkem: {total} řádků (~{total / minutes:.1f}/min; před #616: ~25/min)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="NQ")
    parser.add_argument("--dates", required=True, help="Čárkami oddělená ISO data")
    args = parser.parse_args()
    for date in args.dates.split(","):
        measure(args.symbol, date.strip())


if __name__ == "__main__":
    main()
