"""Měření podílu objemu mimo tisk (#1007, krok 1) — jen analýza, nic se nezapisuje.

Pro každý den a kontrakt porovná rozpětí kumulativního objemu ze snapshotů
(celkový objem vč. noh spreadů a bloků) se součtem velikostí tisků z dxFeed
`TimeAndSale` (`data/trades/`, #795) v témže časovém okně. Rozdíl = objem,
který nikdy nevytiskl trade (strukturovaný a dohodnutý). Záporný rozdíl
(tisků víc než objemu) = nekonzistence feedů; vykazuje se zvlášť.

Porovnává se **per kontrakt a okno**, ne po minutách: snapshoty rotují v
dávkách, takže kontrakt nemá řádek každou minutu — minutový join by tisky bez
protějšku zahazoval a podíl mimo tisk nadsazoval.

Expirace ze streamer symbolu (`./E1CU26C7685:XCME`) se počítá z pravidla CME
týdenních tříd (E{n}A–D = n-tý pondělí–čtvrtek měsíce, EW{n} = n-tý pátek,
EW = poslední obchodní den měsíce, ES = 3. pátek čtvrtletního měsíce) a ověřuje
proti `oi_eod`, kde je trading class vyplněná (`--mapping`, volitelné).

    uv run python scripts/measure_structured_volume.py [--mapping mapping.csv]
"""

from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

MONTH_CODES = "FGHJKMNQUVXZ"
SYMBOL_RE = re.compile(r"^\./([A-Z0-9]+?)([FGHJKMNQUVXZ])(\d\d)([CP])(\d+(?:\.\d+)?):XCME$")
RTH = (dt.time(13, 30), dt.time(20, 0))  # US RTH v UTC (letní čas)
WEEKDAY_BY_LETTER = {"A": 0, "B": 1, "C": 2, "D": 3}


def nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    days = [
        dt.date(year, month, d)
        for d in range(1, calendar.monthrange(year, month)[1] + 1)
        if dt.date(year, month, d).weekday() == weekday
    ]
    return days[n - 1]


def expiry_from_class(root: str, trading_class: str, month: int, year: int) -> str | None:
    """Datum expirace z CME třídy; None pro neznámý tvar."""
    if trading_class == root:  # kvartální (ES, NQ): 3. pátek
        return nth_weekday(year, month, 4, 3).strftime("%Y%m%d")
    m = re.fullmatch(r"E(\d)([ABCD])", trading_class)
    if m:
        return nth_weekday(year, month, WEEKDAY_BY_LETTER[m.group(2)], int(m.group(1))).strftime(
            "%Y%m%d"
        )
    m = re.fullmatch(r"EW(\d)", trading_class)
    if m:
        return nth_weekday(year, month, 4, int(m.group(1))).strftime("%Y%m%d")
    if trading_class == "EW":  # end-of-month: poslední obchodní den měsíce
        last = calendar.monthrange(year, month)[1]
        day = dt.date(year, month, last)
        while day.weekday() >= 5:
            day -= dt.timedelta(days=1)
        return day.strftime("%Y%m%d")
    return None


def load_mapping(path: Path | None) -> dict[tuple[str, str, int, int], str]:
    if path is None:
        return {}
    votes: dict[tuple[str, str, int, int], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    with path.open(encoding="utf-8") as fh:
        for symbol, _date, trading_class, expiry in csv.reader(fh, delimiter="|"):
            if not trading_class:
                continue
            votes[(symbol, trading_class, int(expiry[4:6]), int(expiry[:4]) % 100)][expiry] += 1
    return {key: max(exp.items(), key=lambda kv: kv[1])[0] for key, exp in votes.items()}


def parse_symbol(symbol: str, streamer: str, mapping: dict) -> tuple[str, float, str] | None:
    m = SYMBOL_RE.match(streamer)
    if not m:
        return None
    trading_class, month_code, yy, right, strike = m.groups()
    month, year = MONTH_CODES.index(month_code) + 1, 2000 + int(yy)
    expiry = expiry_from_class(symbol, trading_class, month, year)
    known = mapping.get((symbol, trading_class, month, year % 100))
    if known is not None and expiry is not None and known != expiry:
        raise RuntimeError(
            f"Pravidlo {trading_class} {month}/{year} → {expiry}, oi_eod říká {known}"
        )
    if expiry is None:
        expiry = known
    return (expiry, float(strike), right) if expiry else None


def spot_for_day(data_dir: Path, symbol: str, day: dt.date) -> float | None:
    path = data_dir / "derived" / symbol / "bars" / f"{day.isoformat()}.parquet"
    if not path.exists():
        return None
    bars = pd.read_parquet(path)
    return float(bars["close"].median()) if len(bars) else None


def hourly_stats(snap: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    """Per (kontrakt, hodina): rozpětí objemu mezi prvním a posledním snapshotem
    hodiny a Σ tisků v témže intervalu. Hodina je jednotka, ve které se dá
    poznat, jestli oba feedy žily."""
    snap = snap.assign(hour=snap["ts_min"].dt.floor("h"))
    g = snap.groupby(["expiry", "strike", "right", "hour"])
    bounds = pd.DataFrame(
        {
            "first": g["ts_min"].min(),
            "last": g["ts_min"].max(),
            "span": g["volume"].last() - g["volume"].first(),
        }
    ).reset_index()
    tr = trades.assign(hour=trades["ts"].dt.floor("h")).merge(
        bounds, on=["expiry", "strike", "right", "hour"], how="inner"
    )
    tr = tr[(tr["ts"] > tr["first"]) & (tr["ts"] <= tr["last"] + pd.Timedelta(seconds=59))]
    printed = tr.groupby(["expiry", "strike", "right", "hour"])["size"].sum().rename("printed")
    out = bounds.set_index(["expiry", "strike", "right", "hour"]).join(printed, how="left")
    out["printed"] = out["printed"].fillna(0.0)
    return out[out["span"] >= 0]


def live_hours(stats: pd.DataFrame, tolerance: float = 1.5) -> tuple[set, dict[str, int]]:
    """Hodiny, ve kterých žily oba feedy: objem i tisky > 0 a tisků ≤ tolerance × objem
    (víc tisků než objemu = snapshotový feed stál nebo zaostával)."""
    by_hour = stats.groupby(level="hour")[["span", "printed"]].sum()
    dead_snap = by_hour[(by_hour["span"] <= 0) | (by_hour["printed"] > tolerance * by_hour["span"])]
    dead_trades = by_hour[(by_hour["printed"] <= 0) & (by_hour["span"] > 0)]
    valid = set(by_hour.index) - set(dead_snap.index) - set(dead_trades.index)
    return valid, {
        "hours": len(by_hour),
        "dead_snap": len(dead_snap),
        "dead_trades": len(dead_trades),
    }


def measure_day(data_dir: Path, symbol: str, day: dt.date, mapping: dict) -> dict | None:
    trades_path = data_dir / "trades" / symbol / f"{day.isoformat()}.parquet"
    if not trades_path.exists():
        return None
    trades = pd.read_parquet(trades_path)
    keys = trades["streamer_symbol"].map(lambda s: parse_symbol(symbol, s, mapping))
    unmapped = int(keys.isna().sum())
    trades = trades[keys.notna()].copy()
    keys = keys.dropna()
    trades["expiry"] = keys.map(lambda k: k[0]).values
    trades["strike"] = keys.map(lambda k: k[1]).values
    trades["right"] = keys.map(lambda k: k[2]).values

    frames = []
    for path in sorted((data_dir / "snapshots" / symbol).glob(f"*/{day.isoformat()}.parquet")):
        snap = pd.read_parquet(path, columns=["ts_min", "strike", "right", "volume"])
        frames.append(snap.dropna(subset=["volume"]).assign(expiry=path.parent.name))
    if not frames:
        return None
    snaps = (
        pd.concat(frames, ignore_index=True)
        .sort_values(["expiry", "strike", "right", "ts_min"])
        .drop_duplicates(subset=["expiry", "strike", "right", "ts_min"], keep="last")
    )

    stats = hourly_stats(snaps, trades)
    valid, hour_info = live_hours(stats)
    stats = stats[stats.index.get_level_values("hour").isin(valid)]
    hours = stats.index.get_level_values("hour")
    minute_time = pd.Series(hours.time, index=stats.index)
    rth_mask = (minute_time >= RTH[0]) & (minute_time < RTH[1])
    total = stats
    rth = stats[rth_mask.values]
    eth = stats[~rth_mask.values]

    spot = spot_for_day(data_dir, symbol, day)
    step = 5.0 if symbol == "ES" else 10.0
    strikes = pd.Series(stats.index.get_level_values("strike"), index=stats.index)
    if spot is not None:
        distance = ((strikes - spot).abs() / step).round().astype(int)
        bucket = pd.cut(distance, [-1, 2, 15, 10_000], labels=["ATM±2", "±3..15", ">15"]).astype(
            str
        )
        total = total.assign(bucket=bucket.values)
    else:
        total = total.assign(bucket="?")

    def share(frame: pd.DataFrame) -> tuple[float, float, float]:
        frame = frame[frame["span"] > 0]
        s, p = frame["span"].sum(), frame["printed"].sum()
        return s, p, (s - p) / s if s > 0 else float("nan")

    negative = total[total["printed"] > total["span"]]
    return {
        "day": day,
        "trades": int(len(trades)),
        "unmapped": unmapped,
        "hours": (
            f"{len(valid)}/{hour_info['hours']} "
            f"(snap −{hour_info['dead_snap']}, tisk −{hour_info['dead_trades']})"
        ),
        "neg_contracts": int(len(negative)),
        "neg_volume": float((negative["printed"] - negative["span"]).sum()),
        "total": share(total),
        "rth": share(rth),
        "eth": share(eth),
        "buckets": {b: share(total[total["bucket"] == b]) for b in ("ATM±2", "±3..15", ">15")},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument(
        "--mapping",
        type=Path,
        default=None,
        help="CSV symbol|date|trading_class|expiry z oi_eod (ověření pravidla)",
    )
    parser.add_argument("--symbols", nargs="*", default=["ES", "NQ"])
    args = parser.parse_args(argv)
    mapping = load_mapping(args.mapping)

    for symbol in args.symbols:
        days = sorted(
            dt.date.fromisoformat(p.stem)
            for p in (args.data_dir / "trades" / symbol).glob("*.parquet")
        )
        print(f"\n## {symbol}\n")
        print(
            "| den | tisků | platné hodiny (vyřazeno) | Σ objem | Σ tisk | **mimo tisk** | "
            "tisk > objem (kontrakt-hodin / objem) | RTH | mimo RTH | ATM±2 | ±3..15 | >15 |"
        )
        print("|---|---|---|---|---|---|---|---|---|---|---|---|")
        agg = defaultdict(float)
        for day in days:
            r = measure_day(args.data_dir, symbol, day, mapping)
            if r is None:
                continue
            s, p, sh = r["total"]
            agg["span"] += s
            agg["printed"] += p
            agg["neg"] += r["neg_volume"]
            b = r["buckets"]
            print(
                f"| {r['day']} | {r['trades']:,} | {r['hours']} | {s:,.0f} | {p:,.0f} | "
                f"**{sh:.1%}** | {r['neg_contracts']} / {r['neg_volume']:,.0f} | "
                f"{r['rth'][2]:.1%} | {r['eth'][2]:.1%} | "
                f"{b['ATM±2'][2]:.1%} | {b['±3..15'][2]:.1%} | {b['>15'][2]:.1%} |"
            )
        if agg["span"]:
            share_all = (agg["span"] - agg["printed"]) / agg["span"]
            print(
                f"| **celkem** | | | {agg['span']:,.0f} | {agg['printed']:,.0f} | "
                f"**{share_all:.1%}** | / {agg['neg']:,.0f} | | | | |"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
