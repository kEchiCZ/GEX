"""Měření #602: rezignace long put v den expirace — drift do settle vs. kontrasty.

0DTE expiruje každý den, takže kontrast není „den s/bez expirace", ale trojice
(schváleno 22. 8., okna rozšířena na celé US odpoledne):

A) POLOHA: close v T-X nad put zdí (+1 krok striků) vs. pod/uvnitř — mechanika
   (vadnoucí OTM puty → dealer odkupuje short hedge) predikuje drift jen NAD.
B) DÁVKA: podíl expirující put OI (oi_eod, věčný archiv) — drift má škálovat.
C) ČAS: stejné okno dopoledne (T-2X→T-X) — kontrola obecného odpoledního driftu.

Okna: T-6/T-4/T-2/T-1 h do settle (20:00 UTC). Drift v bodech i ×ATR(14) 1min.

Spuštění (v engine kontejneru; čte parquet + PG):
    python measure_put_resignation.py --symbols ES,NQ
"""

import argparse
import datetime as dt
import glob
import os
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

DATA = Path("data")
WINDOWS_H = [6, 4, 2, 1]
SETTLE_UTC = dt.time(20, 0)
STRIKE_STEP = {"ES": 5.0, "NQ": 10.0}

OI_QUERY = text("""
    select sum(oi) filter (where "right" = 'P' and expiry = :expiry)      as expiring,
           sum(oi) filter (where "right" = 'P')                            as total
    from oi_eod where symbol = :symbol and date = :date
""")


def atr14(bars: pd.DataFrame, at: pd.Timestamp) -> float | None:
    """ATR(14) z 1min barů k okamžiku `at` — týž duch jako mechanika setupů."""
    window = bars[bars.ts_min <= at].tail(15)
    if len(window) < 15:
        return None
    prev_close = window.close.shift(1)
    tr = pd.concat(
        [
            window.high - window.low,
            (window.high - prev_close).abs(),
            (window.low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    value = tr.tail(14).mean()
    return float(value) if pd.notna(value) else None


def close_at(bars: pd.DataFrame, at: pd.Timestamp) -> float | None:
    window = bars[bars.ts_min <= at]
    return float(window.close.iloc[-1]) if len(window) else None


def put_wall_at(levels: pd.DataFrame, at: pd.Timestamp) -> float | None:
    window = levels[levels.ts_min <= at]
    if not len(window):
        return None
    value = window.put_wall.iloc[-1]
    return float(value) if pd.notna(value) else None


def day_frames(symbol: str, date: str) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """(bary, levels 0DTE expirace) pro den; None = den bez dat."""
    bars_file = DATA / "derived" / symbol / "bars" / f"{date}.parquet"
    expiry = date.replace("-", "")
    levels_file = DATA / "derived" / symbol / expiry / "levels" / f"{date}.parquet"
    if not bars_file.exists() or not levels_file.exists():
        return None
    bars = pd.read_parquet(bars_file).sort_values("ts_min")
    levels = pd.read_parquet(levels_file).sort_values("ts_min")
    bars["ts_min"] = pd.to_datetime(bars.ts_min, utc=True)
    levels["ts_min"] = pd.to_datetime(levels.ts_min, utc=True)
    return bars, levels


def expiring_put_share(db, symbol: str, date: str) -> float | None:
    row = db.execute(
        OI_QUERY, {"symbol": symbol, "date": date, "expiry": date.replace("-", "")}
    ).fetchone()
    if row is None or not row.total:
        return None
    return float(row.expiring or 0.0) / float(row.total)


def measure(symbol: str, db) -> pd.DataFrame:
    rows = []
    step = STRIKE_STEP.get(symbol, 5.0)
    for levels_dir in sorted(glob.glob(str(DATA / "derived" / symbol / "*" / "levels"))):
        expiry = Path(levels_dir).parent.name
        date = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
        frames = day_frames(symbol, date)
        if frames is None:
            continue
        bars, levels = frames
        settle = pd.Timestamp(f"{date}T{SETTLE_UTC:%H:%M}", tz="UTC")
        settle_close = close_at(bars, settle)
        if settle_close is None:
            continue
        share = expiring_put_share(db, symbol, date)
        for hours in WINDOWS_H:
            at = settle - pd.Timedelta(hours=hours)
            price = close_at(bars, at)
            wall = put_wall_at(levels, at)
            atr = atr14(bars, at)
            if price is None or wall is None or atr is None or atr <= 0:
                continue
            # Kontrast C: stejné okno bezprostředně před T-X (T-2X → T-X)
            control_from = close_at(bars, settle - pd.Timedelta(hours=2 * hours))
            rows.append(
                {
                    "date": date,
                    "window_h": hours,
                    "above": price >= wall + step,
                    "drift_pts": settle_close - price,
                    "drift_atr": (settle_close - price) / atr,
                    "control_pts": (price - control_from) if control_from is not None else None,
                    "put_share": share,
                }
            )
    return pd.DataFrame(rows)


def report(symbol: str, frame: pd.DataFrame) -> None:
    print(f"\n===== {symbol} — {frame.date.nunique()} seancí =====")
    print("okno  skupina      n   Ø drift b   Ø ×ATR   hit>0   Ø kontrola b")
    for hours in WINDOWS_H:
        window = frame[frame.window_h == hours]
        for above in (True, False):
            group = window[window.above == above]
            if not len(group):
                continue
            label = "nad zdí " if above else "pod/uvnitř"
            control = group.control_pts.dropna()
            print(
                f"T-{hours}h  {label}  {len(group):>3}   {group.drift_pts.mean():+8.2f}   "
                f"{group.drift_atr.mean():+6.2f}   {(group.drift_pts > 0).mean():5.0%}   "
                f"{control.mean():+8.2f}"
                if len(control)
                else ""
            )
    # Kontrast B: terciliy dávky na okně T-4 (jen skupina nad zdí)
    dose = frame[(frame.window_h == 4) & frame.above & frame.put_share.notna()].copy()
    if len(dose) >= 6:
        dose["tercil"] = pd.qcut(dose.put_share, 3, labels=["malá", "střední", "velká"])
        print("\n  Dávka (podíl expirující put OI, T-4h nad zdí):")
        for label, group in dose.groupby("tercil", observed=True):
            print(
                f"    {label:<8} n={len(group):>2}  Ø share {group.put_share.mean():.2f}  "
                f"Ø drift {group.drift_pts.mean():+.2f} b ({group.drift_atr.mean():+.2f} ATR)"
            )
    else:
        print(f"\n  Dávka: jen {len(dose)} dnů nad zdí s OI — na terciliy málo, vypisuji korelaci")
        if len(dose) >= 3:
            print(f"    korelace share×drift: {dose.put_share.corr(dose.drift_pts):+.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="ES,NQ")
    args = parser.parse_args()
    url = os.environ.get("GEXLENS_DATABASE_URL")
    if not url:
        raise SystemExit("Chybí GEXLENS_DATABASE_URL")
    engine = create_engine(url)
    with engine.connect() as db:
        for symbol in args.symbols.split(","):
            frame = measure(symbol.strip(), db)
            if len(frame):
                report(symbol.strip(), frame)
            else:
                print(f"{symbol}: žádná použitelná data")


if __name__ == "__main__":
    main()
