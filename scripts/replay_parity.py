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

Shoda sloupce počítá jen minuty, kde obě strany mají hodnotu a sedí; NaN × NaN
shoda není (dvě chybějící hodnoty nic nedokládají) a vypisuje se zvlášť (#1590).

Brána parity stín ↔ živá 1A (ADR-0057 bod 7, #1590): `--cum-source stin` pod
mechanikou v6 postaví CumΔ replaye ze stínu `cumdelta_dx` i v seancích s 1A,
touž cestou jako historie 21. 9. → nasazení (štítek L → minuta L + 1). Proti
živému feature logu (1A) se měří mezery (minuta s mezerou jen v jedné řadě)
a 10min přírůstky CumΔ (Pearson r a shoda znaménka) — okno T1 a sklonu.

Spuštění (engine kontejner):  python replay_parity.py --symbol ES --date 2026-08-24
Brána po bumpu v6 (seance čárkou):
    python replay_parity.py --symbol ES --mechanics 6 --cum-source stin
        --date 2026-11-02,2026-11-03,2026-11-04,2026-11-05,2026-11-06
"""

import argparse
import datetime as dt
import math
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_setups import DATA, build_minutes, load_series  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from gexlens_engine.compute.bandregime import band_context  # noqa: E402
from gexlens_engine.compute.gexfield import GexProfile  # noqa: E402
from gexlens_engine.compute.setups import (  # noqa: E402
    SETUP_MECHANICS_VERSION,
    SetupParams,
    average_true_range,
)
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
#: Interpolované hrany gamma masy nesou float32 stopu z cesty profilu —
#: rozdíl na 7. platné číslici (~1e-4 b na hladině 7600). Pro trénink šum,
#: proto relativní tolerance místo honu na bitovou shodu.
RTOL_COLUMNS = {"gamma_edge_up": 1e-6, "gamma_edge_dn": 1e-6}
#: Známé rozdílové sloupce (jiný zdroj živě vs. replay) — reportují se zvlášť
KNOWN_DIFF = {"call_flow", "put_flow", "opt_vol"}
#: Okno přírůstků CumΔ brány stín ↔ 1A v minutách — okno T1 a sklonu (ADR-0057 bod 4)
WINDOW_MIN = 10


@dataclass(frozen=True)
class CumParity:
    """Parita CumΔ jedné seance: replay (stín) × živý feature log (1A)."""

    minutes: int
    #: minuty s mezerou jen v jedné řadě (neshoda) a v obou (NaN × NaN, shoda to není)
    gap_one_side: int
    gap_both: int
    #: přírůstky přes okno: počet oken, Pearson r, podíl oken se stejným znaménkem
    windows: int
    r: float | None
    sign_share: float | None


def cum_parity(live: pd.Series, replay: pd.Series, minutes: int = WINDOW_MIN) -> CumParity:
    """Mezery a `minutes`-minutové přírůstky CumΔ dvou řad (index = minuta, NaN = mezera).

    Okno (t − minutes, t] se počítá, jen když obě řady mají hodnotu v t i t − minutes
    a žádný jejich řádek v okně není NaN — přírůstek přes mezeru úplný není
    (`_cum_levels`). Znaménko se srovnává jen u oken, kde jsou oba přírůstky nenulové.
    """
    common = live.index.intersection(replay.index)
    frame = pd.DataFrame({"live": live[common], "replay": replay[common]}).sort_index()
    nan_live, nan_replay = frame["live"].isna(), frame["replay"].isna()
    step = pd.Timedelta(minutes=minutes)
    times = list(frame.index)
    position = {ts: index for index, ts in enumerate(times)}
    pairs: list[tuple[float, float]] = []
    for index, ts in enumerate(times):
        start = position.get(ts - step)
        if start is None:
            continue
        window = frame.iloc[start : index + 1]
        if window.isna().to_numpy().any():
            continue
        pairs.append(
            (
                float(window["live"].iloc[-1] - window["live"].iloc[0]),
                float(window["replay"].iloc[-1] - window["replay"].iloc[0]),
            )
        )
    r: float | None = None
    if len(pairs) >= 3:
        try:
            r = statistics.correlation([a for a, _ in pairs], [b for _, b in pairs])
        except statistics.StatisticsError:  # konstantní řada
            r = None
    signed = [(a, b) for a, b in pairs if a != 0.0 and b != 0.0]
    sign_share = sum(1 for a, b in signed if (a > 0) == (b > 0)) / len(signed) if signed else None
    return CumParity(
        minutes=len(frame),
        gap_one_side=int((nan_live ^ nan_replay).sum()),
        gap_both=int((nan_live & nan_replay).sum()),
        windows=len(pairs),
        r=r,
        sign_share=sign_share,
    )


def replay_features(
    symbol: str,
    expiry: str,
    repo: OIEodRepository,
    *,
    prints: bool = SETUP_MECHANICS_VERSION >= 6,
    shadow_only: bool = False,
) -> pd.DataFrame:
    """Feature řádky z replaye — týmiž funkcemi jako živý `_log_features`."""
    minutes = build_minutes(symbol, expiry, repo, prints, shadow_only=shadow_only)
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


def compare(live: pd.DataFrame, replay: pd.DataFrame) -> CumParity:
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
    columns = [
        *DIRECT_FIELDS,
        "gex_regime",
        "atr",
        "band_sharpness",
        "band_sharpness_pct",
        "band_depth",
    ]
    for name in columns:
        a, b = merged[f"{name}_live"], merged[f"{name}_replay"]
        if name == "gex_regime":
            match = (a.fillna("∅") == b.fillna("∅")).mean()
            print(f"{name:<20} {match:>6.1%}  {'—':>12}")
            continue
        both = a.notna() & b.notna()
        diff = (a[both] - b[both]).abs()
        tolerance = ATOL + RTOL_COLUMNS.get(name, 0.0) * b[both].abs()
        nan_match = (a.isna() == b.isna()).mean()
        both_nan = int((a.isna() & b.isna()).sum())
        # NaN × NaN shoda není (#1590): dvě chybějící hodnoty nic nedokládají
        match = (diff <= tolerance).sum() / len(merged) if len(merged) else 0.0
        note = "known-diff (jiný zdroj)" if name in KNOWN_DIFF else ""
        if nan_match < 1.0:
            note += f" NaN neshoda {1 - nan_match:.1%}"
        if both_nan:
            note += f" NaN v obou {both_nan}"
        print(f"{name:<20} {match:>6.1%}  {diff.max() if len(diff) else 0:>12.6g}  {note}")
    indexed = merged.set_index("ts")
    cum = cum_parity(indexed["cum_delta_live"], indexed["cum_delta_replay"])
    print(
        f"CumΔ {WINDOW_MIN}min přírůstky: oken {cum.windows}, r {_fmt(cum.r)}, "
        f"shoda znaménka {_fmt(cum.sign_share, pct=True)}; mezera jen v jedné řadě "
        f"{cum.gap_one_side} z {cum.minutes} min, v obou {cum.gap_both}"
    )
    return cum


def _fmt(value: float | None, *, pct: bool = False) -> str:
    if value is None or math.isnan(value):
        return "—"
    return f"{value:.1%}" if pct else f"{value:.3f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="ES")
    parser.add_argument("--date", required=True, help="seance, víc čárkou (brána ≥ 5 seancí)")
    parser.add_argument(
        "--mechanics",
        type=int,
        choices=(5, 6),
        default=SETUP_MECHANICS_VERSION,
        help="mechanika replaye: 5 = CumΔ midpoint, 6 = CumΔ z tisků (ADR-0057 bod 7)",
    )
    parser.add_argument(
        "--cum-source",
        choices=("mechanika", "stin"),
        default="mechanika",
        help="stin = pod v6 CumΔ ze stínu i v seancích s 1A (brána parity, ADR-0057 bod 7)",
    )
    parser.add_argument(
        "--shift-min",
        type=int,
        default=0,
        help="Posun replay ts (min). Po opravě decision-time konvence v "
        "build_minutes (bar N-1 + stav N jako živý engine) je default 0",
    )
    args = parser.parse_args()
    prints = args.mechanics >= 6
    shadow_only = args.cum_source == "stin"
    if shadow_only and not prints:
        parser.error("--cum-source stin jen s --mechanics 6 (stín je zdroj CumΔ z tisků)")
    settings = load_settings()
    repo = OIEodRepository(create_engine(settings.database_url))
    results: list[CumParity] = []
    for date in [d.strip() for d in args.date.split(",") if d.strip()]:
        expiry = date.replace("-", "")
        features_file = Path(DATA) / args.symbol / "features" / f"{date}.parquet"
        if not features_file.exists():
            raise SystemExit(f"Živý feature log neexistuje: {features_file}")
        live = pd.read_parquet(features_file)
        # Živý log nese expiraci aktivního runtime — replay rekonstruuje tutéž
        expiry = str(live["expiry"].iloc[-1]) if "expiry" in live and len(live) else expiry
        replay = replay_features(args.symbol, expiry, repo, prints=prints, shadow_only=shadow_only)
        replay["ts"] = pd.to_datetime(replay.ts, utc=True) + pd.Timedelta(minutes=args.shift_min)
        day_replay = replay[replay.ts.dt.date == dt.date.fromisoformat(date)]
        print(
            f"=== Parita {args.symbol} {date} (expirace {expiry}, mechanika "
            f"v{args.mechanics}, CumΔ {args.cum_source}) ==="
        )
        results.append(compare(live, day_replay))
    if len(results) > 1:
        rs = [c.r for c in results if c.r is not None]
        signs = [c.sign_share for c in results if c.sign_share is not None]
        minutes = sum(c.minutes for c in results)
        one_side = sum(c.gap_one_side for c in results) / minutes if minutes else None
        print(
            f"=== Souhrn {len(results)} seancí: medián r "
            f"{_fmt(statistics.median(rs) if rs else None)}, nejnižší r "
            f"{_fmt(min(rs) if rs else None)}, nejnižší shoda znaménka "
            f"{_fmt(min(signs) if signs else None, pct=True)}, mezera jen v jedné řadě "
            f"{_fmt(one_side, pct=True)} ==="
        )


if __name__ == "__main__":
    main()
