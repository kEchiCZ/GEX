"""Parita replay × živý feature log (#794 fáze 1).

Samoučící smyčka se bude učit replayem nad archivem — a replay smí být
zdrojem pravdy jen tehdy, když na týchž minutách vyrábí TATÁŽ čísla jako
živý engine. Tenhle skript rekonstruuje vstupní vektor detektoru z partic
(bars + levels + walldom + flow + snapshots + gexprofile + oi_eod, cesta
`backtest_setups.build_minutes`) a porovná ho sloupec po sloupci s živým
`derived/{sym}/features/{date}.parquet` (#796).

Očekávané kategorie:
* bitová shoda: OHLC, flip/zdi/dominance, cum_delta, gamma_edges, max_pain,
* dopočtené: ATR, band metriky (týž vzorec nad týmiž daty → shoda ~1e-9),
* known-diff: call/put_flow a opt_vol — živě z sweep cache, replay z diffu
  snapshot volume; drobné rozdíly na minutách s rotací jsou očekávané a
  REPORTUJÍ se (parita je měření, ne přání).

Spuštění (engine kontejner):  python replay_parity.py --symbol ES --date 2026-08-24
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_setups import DATA, build_minutes, load_series  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from gexlens_engine.compute.bandregime import band_context  # noqa: E402
from gexlens_engine.compute.gexfield import GexProfile  # noqa: E402
from gexlens_engine.compute.setups import SetupParams, average_true_range  # noqa: E402
from gexlens_engine.config import load_settings  # noqa: E402
from gexlens_engine.storage.oi_archive import OIEodRepository  # noqa: E402

#: Sloupce feature logu vs. atributy MinuteInputs (shodná jména)
DIRECT_FIELDS = [
    "open", "high", "low", "close", "flip", "call_wall", "put_wall", "max_pain",
    "cum_delta", "call_flow", "put_flow", "opt_vol", "minutes_to_expiry",
    "call_wall_dom", "put_wall_dom", "gamma_edge_up", "gamma_edge_dn",
]  # fmt: skip
#: Tolerance floatů (dopočty přes numpy vs. čisté pythony)
ATOL = 1e-9
#: Známé rozdílové sloupce (jiný zdroj živě vs. replay) — reportují se zvlášť
KNOWN_DIFF = {"call_flow", "put_flow", "opt_vol"}


def replay_features(symbol: str, expiry: str, repo: OIEodRepository) -> pd.DataFrame:
    """Feature řádky z replaye — týmiž funkcemi jako živý `_log_features`."""
    minutes = build_minutes(symbol, expiry, repo)
    profiles = load_series(f"{DATA}/{symbol}/{expiry}/gexprofile/*.parquet")
    profile_map: dict[object, GexProfile] = {}
    if profiles is not None:
        for row in profiles.itertuples():
            profile_map[pd.Timestamp(row.ts_min)] = GexProfile(
                ts_min=row.ts_min,
                grid_start=float(row.grid_start),
                grid_step=float(row.grid_step),
                values=tuple(float(v) for v in row.values),
            )
    params = SetupParams()
    rows = []
    history = []
    for inputs in minutes:
        history.append(inputs)
        profile = profile_map.get(pd.Timestamp(inputs.ts))
        band = band_context(profile, inputs.close)
        rows.append(
            {
                "ts": inputs.ts,
                **{name: getattr(inputs, name) for name in DIRECT_FIELDS},
                "gex_regime": inputs.gex_regime,
                "atr": average_true_range(history, params.atr_lookback),
                "band_sharpness": band.get("band_sharpness"),
                "band_sharpness_pct": band.get("band_sharpness_pct"),
                "band_depth": band.get("band_depth"),
            }
        )
    return pd.DataFrame(rows)


def compare(live: pd.DataFrame, replay: pd.DataFrame) -> None:
    live = live.copy()
    live["ts"] = pd.to_datetime(live.ts, utc=True)
    replay["ts"] = pd.to_datetime(replay.ts, utc=True)
    merged = live.merge(replay, on="ts", how="inner", suffixes=("_live", "_replay"))
    only_live = len(live) - len(merged)
    only_replay = len(replay) - len(merged)
    print(
        f"minut živě {len(live)}, replay {len(replay)}, společných {len(merged)} "
        f"(jen živě {only_live}, jen replay {only_replay})"
    )
    print(f"{'sloupec':<20} {'shoda':>7}  {'max |Δ|':>12}  pozn.")
    columns = [*DIRECT_FIELDS, "gex_regime", "atr", "band_sharpness", "band_sharpness_pct", "band_depth"]  # fmt: skip
    for name in columns:
        a, b = merged[f"{name}_live"], merged[f"{name}_replay"]
        if name == "gex_regime":
            match = (a.fillna("∅") == b.fillna("∅")).mean()
            print(f"{name:<20} {match:>6.1%}  {'—':>12}")
            continue
        both = a.notna() & b.notna()
        diff = (a[both] - b[both]).abs()
        nan_match = (a.isna() == b.isna()).mean()
        match = ((diff <= ATOL).sum() + (a.isna() & b.isna()).sum()) / len(merged)
        note = "known-diff (jiný zdroj)" if name in KNOWN_DIFF else ""
        if nan_match < 1.0:
            note += f" NaN neshoda {1 - nan_match:.1%}"
        print(f"{name:<20} {match:>6.1%}  {diff.max() if len(diff) else 0:>12.6g}  {note}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="ES")
    parser.add_argument("--date", required=True)
    parser.add_argument(
        "--shift-min",
        type=int,
        default=1,
        help="Posun replay ts (min). Živý log razítkuje minutu N barem N-1 — "
        "změřeno 24. 8.: +1 dává 99,5%% shodu, 0 jen ~15%% (nález #794 f. 1)",
    )
    args = parser.parse_args()
    expiry = args.date.replace("-", "")
    features_file = Path(DATA) / args.symbol / "features" / f"{args.date}.parquet"
    if not features_file.exists():
        raise SystemExit(f"Živý feature log neexistuje: {features_file}")
    live = pd.read_parquet(features_file)
    # Živý log nese expiraci aktivního runtime — replay rekonstruuje tutéž
    expiry = str(live["expiry"].iloc[-1]) if "expiry" in live and len(live) else expiry
    settings = load_settings()
    repo = OIEodRepository(create_engine(settings.database_url))
    replay = replay_features(args.symbol, expiry, repo)
    replay["ts"] = pd.to_datetime(replay.ts, utc=True) + pd.Timedelta(minutes=args.shift_min)
    day_replay = replay[replay.ts.dt.date == dt.date.fromisoformat(args.date)]
    print(f"=== Parita {args.symbol} {args.date} (expirace {expiry}) ===")
    compare(live, day_replay)


if __name__ == "__main__":
    main()
