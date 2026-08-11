"""Měření průrazů hranice gamma masy nad historií (#601 fáze 1).

Odpovídá na jedinou otázku: **stojí za to z průrazu hranice dělat šablonu setupu?**
Nic nedetekuje ani neukládá — jen spočítá, jak často cena hranici z `compute.gexfield.
gamma_edges` protne a co se stane potom.

Hranice se dopočítávají z uložené řady `gexprofile` TOUTÉŽ funkcí, jakou používá
`SetupEngine` živě (#600), takže měření a produkce nemůžou tiše utéct od sebe.

Metodika:
* **Průraz** = close minuty přejde z „uvnitř masy" na „za hranicí" (nad `up`, pod `dn`).
  Bere se první minuta přechodu; dokud se cena nevrátí dovnitř, další průraz té strany
  se nepočítá (jinak by jeden pohyb generoval desítky událostí).
* **Následný pohyb** se měří PO SMĚRU průrazu v +5/+15/+30 min: `move` je změna close,
  `mfe` největší příznivý a `mae` největší nepříznivý výkyv uvnitř okna (z high/low).
* **Falešný průraz** = cena je do 15 minut zpátky uvnitř masy A pohyb po směru je ≤ 0.

Spuštění: `uv run python scripts/measure_gamma_edges.py --symbols ES NQ`
"""

import argparse
import datetime as dt
import glob
import statistics
from dataclasses import dataclass

import pandas as pd

from gexlens_engine.compute.gexfield import GAMMA_EDGE_SHARE, GexProfile, gamma_edges

DATA = "data/derived"
# Okna, ve kterých se měří následný pohyb (minuty)
HORIZONS = (5, 15, 30)
# Do kolika minut se návrat dovnitř masy počítá jako falešný průraz
FALSE_BREAK_WINDOW = 15


@dataclass(frozen=True)
class Break:
    symbol: str
    expiry: str
    ts: dt.datetime
    side: str  # "up" | "dn"
    edge: float
    close: float
    moves: dict[int, float]
    mfe: dict[int, float]
    mae: dict[int, float]
    returned: bool


def load_series(pattern: str) -> pd.DataFrame | None:
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    frame = pd.concat([pd.read_parquet(f) for f in files])
    return frame.sort_values("ts_min").drop_duplicates("ts_min", keep="last")


def edges_by_minute(
    symbol: str, expiry: str, share: float
) -> tuple[dict[dt.datetime, tuple[float, float]], int, int]:
    """{minuta: (dn, up)} z uložených Dyn GEX profilů + počty (celkem, na kraji mřížky).

    Hranice ležící NA KRAJI mřížky se zahazuje: znamená, že gamma masa mřížku
    přesahuje a skutečná hranice leží dál. Průraz takové „hranice" je jen výjezd
    ceny z rozsahu mřížky, ne opuštění masy — počítat ho by měřilo úplně něco
    jiného, než co se měřit má.
    """
    frame = load_series(f"{DATA}/{symbol}/{expiry}/gexprofile/*.parquet")
    if frame is None:
        return {}, 0, 0
    result: dict[dt.datetime, tuple[float, float]] = {}
    total = 0
    clipped = 0
    for row in frame.itertuples():
        ts = pd.Timestamp(row.ts_min).to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.UTC)
        profile = GexProfile(
            ts_min=ts,
            grid_start=float(row.grid_start),
            grid_step=float(row.grid_step),
            values=tuple(float(v) for v in row.values),
        )
        edges = gamma_edges(profile, share=share)
        if edges.up is None or edges.dn is None:
            continue
        total += 1
        top = profile.grid_start + (len(profile.values) - 1) * profile.grid_step
        if abs(edges.up - top) < 1e-9 or abs(edges.dn - profile.grid_start) < 1e-9:
            clipped += 1
            continue
        result[ts] = (edges.dn, edges.up)
    return result, total, clipped


def breaks_for(symbol: str, expiry: str, share: float) -> tuple[list[Break], int, int]:
    edges, total, clipped = edges_by_minute(symbol, expiry, share)
    bars = load_series(f"{DATA}/{symbol}/bars/*.parquet")
    if not edges or bars is None:
        return [], total, clipped
    bars = bars.copy()
    bars["ts"] = [
        pd.Timestamp(t).to_pydatetime().replace(tzinfo=dt.UTC)
        if pd.Timestamp(t).tzinfo is None
        else pd.Timestamp(t).to_pydatetime()
        for t in bars.ts_min
    ]
    # Jen minuty, ke kterým existuje profil (den expirace, ne celá historie barů)
    bars = bars[bars.ts.isin(edges.keys())].reset_index(drop=True)
    if bars.empty:
        return [], total, clipped

    closes = bars.close.astype(float).tolist()
    highs = bars.high.astype(float).tolist()
    lows = bars.low.astype(float).tolist()
    stamps = bars.ts.tolist()

    found: list[Break] = []
    outside = {"up": False, "dn": False}
    for index, ts in enumerate(stamps):
        dn, up = edges[ts]
        close = closes[index]
        for side, beyond, edge in (("up", close > up, up), ("dn", close < dn, dn)):
            if not beyond:
                outside[side] = False
                continue
            if outside[side]:
                continue  # pořád tentýž pohyb, ne nový průraz
            outside[side] = True
            sign = 1.0 if side == "up" else -1.0
            moves, mfe, mae = {}, {}, {}
            for horizon in HORIZONS:
                stop = min(index + horizon, len(closes) - 1)
                moves[horizon] = sign * (closes[stop] - close)
                window_high = max(highs[index : stop + 1])
                window_low = min(lows[index : stop + 1])
                # Příznivý/nepříznivý výkyv PO SMĚRU průrazu — kladná čísla
                mfe[horizon] = (window_high - close) if sign > 0 else (close - window_low)
                mae[horizon] = (close - window_low) if sign > 0 else (window_high - close)
            # Vrátila se cena dovnitř masy?
            returned = False
            for ahead in range(index + 1, min(index + FALSE_BREAK_WINDOW + 1, len(stamps))):
                a_dn, a_up = edges[stamps[ahead]]
                if (side == "up" and closes[ahead] <= a_up) or (
                    side == "dn" and closes[ahead] >= a_dn
                ):
                    returned = True
                    break
            found.append(
                Break(
                    symbol=symbol,
                    expiry=expiry,
                    ts=ts,
                    side=side,
                    edge=edge,
                    close=close,
                    moves=moves,
                    mfe=mfe,
                    mae=mae,
                    returned=returned,
                )
            )
    return found, total, clipped


def describe(rows: list[Break]) -> None:
    if not rows:
        print("  (žádný průraz)")
        return
    dni = len({row.ts.date() for row in rows})
    print(f"  průrazů: {len(rows)} za {dni} dní ({len(rows) / dni:.1f}/den)")
    for horizon in HORIZONS:
        moves = [row.moves[horizon] for row in rows]
        wins = sum(1 for m in moves if m > 0)
        print(
            f"  +{horizon:>2} min: medián {statistics.median(moves):+6.2f} b · "
            f"průměr {statistics.fmean(moves):+6.2f} b · po směru {wins / len(moves):5.0%} · "
            f"MFE {statistics.median([r.mfe[horizon] for r in rows]):5.2f} · "
            f"MAE {statistics.median([r.mae[horizon] for r in rows]):5.2f}"
        )
    false_breaks = sum(1 for row in rows if row.returned and row.moves[15] <= 0)
    share = false_breaks / len(rows)
    print(f"  falešných (návrat do {FALSE_BREAK_WINDOW} min a pohyb ≤ 0): {share:.0%}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["ES", "NQ"])
    parser.add_argument("--share", type=float, default=GAMMA_EDGE_SHARE)
    args = parser.parse_args()

    print(f"Práh hranice: {args.share:.0%} z maxima |NetGEX| profilu\n")
    for symbol in args.symbols:
        expiries = sorted(
            path.split("/")[-1].split("\\")[-1] for path in glob.glob(f"{DATA}/{symbol}/2*")
        )
        rows: list[Break] = []
        minutes_total = minutes_clipped = 0
        for expiry in expiries:
            found, total, clipped = breaks_for(symbol, expiry, args.share)
            rows.extend(found)
            minutes_total += total
            minutes_clipped += clipped
        print(f"── {symbol} ({len(expiries)} expirací) ──")
        share_clipped = minutes_clipped / minutes_total if minutes_total else 0.0
        print(
            f" minut s profilem: {minutes_total} · z toho hranice NA KRAJI mřížky "
            f"(nepoužitelná): {share_clipped:.0%}"
        )
        for side, label in (("up", "nahoru"), ("dn", "dolů")):
            print(f" {label}:")
            describe([row for row in rows if row.side == side])
        print()


if __name__ == "__main__":
    main()
