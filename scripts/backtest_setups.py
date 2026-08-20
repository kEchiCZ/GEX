"""Offline přehrání historie přes PRODUKČNÍ detektor setupů (#434).

Nepřepisuje logiku: importuje `detect_all`, `evaluate_bar`, `r_result` z
`gexlens_engine.compute.setups` a jen kolem nich staví orchestraci, kterou
jinak dělá `SetupEngine` (anti-spam per šablona, blokace směru po sérii stopů,
cooldown v kontra-režimu, vyhodnocení otevřených setupů po barech).

Opční toky (`call_flow` / `put_flow` / `opt_vol`) se **rekonstruují ze snapshotů**
(`data/snapshots/{symbol}/{expiry}/*.parquet`), ne z předpočítané řady — engine
je nikam neukládá, počítá si je za běhu z cache kotací. Snapshot ale nese per
minutu a kontrakt `volume` i `delta`, což jsou přesně vstupy `SetupEngine._flows`:
přírůstek volume proti předchozí minutě téhož kontraktu, vážený |delta|, sečtený
zvlášť za call a put strany (`opt_vol` je nevážený součet přírůstků). Záporné
přírůstky (reset volume na přelomu seance) se zahazují, stejně jako v produkci.
"""

import argparse
import dataclasses
import datetime as dt
import glob
import json
import os
import sys
from collections import defaultdict

import pandas as pd
from sqlalchemy import create_engine

from gexlens_engine.compute.gexfield import GexProfile, gamma_edges
from gexlens_engine.compute.setups import (
    Direction,
    MinuteInputs,
    Outcome,
    SetupParams,
    detect_all,
    evaluate_bar,
    gex_regime,
    is_counter_regime,
    max_pain_strike,
    r_result,
)
from gexlens_engine.storage.oi_archive import OIEodRepository

DATA = "data/derived"


def load_series(pattern: str) -> pd.DataFrame | None:
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    frame = pd.concat([pd.read_parquet(f) for f in files])
    return frame.sort_values("ts_min").drop_duplicates("ts_min", keep="last")


def option_flows(symbol: str, expiry: str) -> pd.DataFrame | None:
    """Δ-vážený opční tok per minuta ze snapshotů — zrcadlo `SetupEngine._flows`.

    Engine porovnává `snapshot.volume` proti hodnotě téhož kontraktu z minulé
    minuty; tady je totéž jako `groupby(strike, right).shift()` nad uloženou
    maticí. Kontrakt, který se ve sweepu poprvé objeví, přírůstek nemá (NaN →
    zahodit) — shodné s `previous is None: continue` v produkci.
    """
    files = sorted(glob.glob(f"data/snapshots/{symbol}/{expiry}/*.parquet"))
    if not files:
        return None
    snap = pd.concat(
        [
            pd.read_parquet(f, columns=["ts_min", "strike", "right", "volume", "delta"])
            for f in files
        ]
    )
    if snap.empty:
        return None
    snap = snap.sort_values(["strike", "right", "ts_min"])
    previous = snap.groupby(["strike", "right"], sort=False)["volume"].shift()
    increment = (snap["volume"] - previous).where(lambda s: s > 0)  # ≤ 0 se přeskakuje
    snap = snap.assign(inc=increment, weighted=increment * snap["delta"].abs())
    grouped = snap.groupby(["ts_min", "right"], sort=True)[["inc", "weighted"]].sum()
    wide = grouped.unstack("right")
    return pd.DataFrame(
        {
            "ts_min": wide.index,
            "call_flow": wide.get(("weighted", "C"), pd.Series(0.0, index=wide.index)).fillna(0.0),
            "put_flow": wide.get(("weighted", "P"), pd.Series(0.0, index=wide.index)).fillna(0.0),
            "opt_vol": wide["inc"].sum(axis=1).fillna(0.0),
        }
    ).reset_index(drop=True)


def max_pain_for(repo: OIEodRepository, symbol: str, expiry: str, day: dt.date) -> float | None:
    try:
        records = repo.values_for(symbol, expiry, day)
    except Exception:
        return None
    oi_map = {(r.strike, r.right): r.oi for r in records}
    return max_pain_strike(oi_map) if oi_map else None


def build_minutes(symbol: str, expiry: str, repo: OIEodRepository) -> list[MinuteInputs]:
    """MinuteInputs jednoho obchodního dne (expirace = adresář derived)."""
    bars = load_series(f"{DATA}/{symbol}/bars/*.parquet")
    levels = load_series(f"{DATA}/{symbol}/{expiry}/levels/*.parquet")
    dom = load_series(f"{DATA}/{symbol}/{expiry}/walldom/*.parquet")
    flow = load_series(f"{DATA}/{symbol}/flow/*.parquet")
    if bars is None or levels is None:
        return []
    frame = bars.merge(levels, on="ts_min", how="inner")
    if dom is not None:
        frame = frame.merge(dom, on="ts_min", how="left")
    if flow is not None:
        frame = frame.merge(flow[["ts_min", "cum_delta"]], on="ts_min", how="left")
    flows = option_flows(symbol, expiry)
    if flows is not None:
        frame = frame.merge(flows, on="ts_min", how="left")
    if frame.empty:
        return []
    # Hranice gamma masy (#600) z uložených Dyn GEX profilů — dřív harness
    # tohle pole nechával None, takže se live vs. replay lišily (#796)
    edges_by_ts: dict[object, tuple[float | None, float | None]] = {}
    profiles = load_series(f"{DATA}/{symbol}/{expiry}/gexprofile/*.parquet")
    if profiles is not None:
        for prof in profiles.itertuples():
            gp = GexProfile(
                ts_min=prof.ts_min,
                grid_start=float(prof.grid_start),
                grid_step=float(prof.grid_step),
                values=tuple(float(v) for v in prof.values),
            )
            edges = gamma_edges(gp)
            edges_by_ts[prof.ts_min] = (edges.up, edges.dn)
    day = pd.Timestamp(frame.ts_min.iloc[-1]).date()
    pain = max_pain_for(repo, symbol, expiry, day)
    settle = dt.datetime.strptime(expiry, "%Y%m%d").replace(hour=20, tzinfo=dt.UTC)

    minutes: list[MinuteInputs] = []
    for row in frame.itertuples():
        ts = pd.Timestamp(row.ts_min).to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.UTC)
        left = (settle - ts).total_seconds() / 60.0
        minutes.append(
            MinuteInputs(
                ts=ts,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                flip=none_if_nan(getattr(row, "flip", None)),
                call_wall=none_if_nan(getattr(row, "call_wall", None)),
                put_wall=none_if_nan(getattr(row, "put_wall", None)),
                max_pain=pain,
                cum_delta=float(getattr(row, "cum_delta", 0.0) or 0.0),
                call_flow=float(getattr(row, "call_flow", 0.0) or 0.0),
                put_flow=float(getattr(row, "put_flow", 0.0) or 0.0),
                opt_vol=float(getattr(row, "opt_vol", 0.0) or 0.0),
                minutes_to_expiry=left if left > 0 else None,
                call_wall_dom=none_if_nan(getattr(row, "call_wall_dom", None)),
                put_wall_dom=none_if_nan(getattr(row, "put_wall_dom", None)),
                gex_regime=gex_regime(
                    float(row.close),
                    none_if_nan(getattr(row, "flip", None)),
                    float(getattr(row, "total_gex", 0.0) or 0.0),
                ),
                gamma_edge_up=edges_by_ts.get(row.ts_min, (None, None))[0],
                gamma_edge_dn=edges_by_ts.get(row.ts_min, (None, None))[1],
            )
        )
    return minutes


def none_if_nan(value):
    if value is None:
        return None
    try:
        return None if pd.isna(value) else float(value)
    except (TypeError, ValueError):
        return None


@dataclasses.dataclass
class OpenSetup:
    template: str
    direction: Direction
    entry: float
    target: float
    stop: float
    created: dt.datetime
    counter: bool


def replay(minutes: list[MinuteInputs], params: SetupParams) -> list[dict]:
    """Přehraje den; vrací uzavřené i otevřené setupy s výsledkem v R."""
    history: list[MinuteInputs] = []
    open_setups: list[OpenSetup] = []
    done: list[dict] = []
    last_created: dict[str, dt.datetime] = {}
    last_counter_stop: dict[str, dt.datetime] = {}
    dir_stops: dict[str, int] = defaultdict(int)
    dir_blocked: dict[str, dt.datetime] = {}

    for now in minutes:
        history.append(now)
        # 1) Vyhodnocení otevřených (stop-first uvnitř svíčky, jako SetupEngine)
        still: list[OpenSetup] = []
        for item in open_setups:
            outcome = evaluate_bar(
                item.direction, item.entry, item.target, item.stop, now.high, now.low
            )
            if outcome is None:
                still.append(item)
                continue
            exit_price = item.stop if outcome is Outcome.STOP else item.target
            result = r_result(item.direction, item.entry, item.stop, exit_price)
            done.append(
                {
                    "template": item.template,
                    "direction": item.direction.value,
                    "created": item.created,
                    "closed": now.ts,
                    "outcome": outcome.value,
                    "r": result,
                }
            )
            side = item.direction.value
            if outcome is Outcome.STOP:
                if item.counter:
                    last_counter_stop[item.template] = now.ts
                dir_stops[side] += 1
                if dir_stops[side] >= params.max_stops_per_direction:
                    dir_blocked[side] = now.ts + dt.timedelta(
                        minutes=params.direction_block_minutes
                    )
            else:
                dir_stops[side] = 0
                dir_blocked.pop(side, None)
        open_setups = still

        # 2) Nové kandidáty přes produkční detect_all
        open_templates = {item.template for item in open_setups}
        for candidate in detect_all(history, params):
            template = candidate.template.value
            if template in open_templates:
                continue
            last = last_created.get(template)
            if last is not None and (now.ts - last).total_seconds() < params.cooldown_minutes * 60:
                continue
            blocked = dir_blocked.get(candidate.direction.value)
            if blocked is not None and now.ts < blocked:
                continue
            counter = is_counter_regime(candidate.direction, candidate.context.get("gex_regime"))
            if counter:
                stop_ts = last_counter_stop.get(template)
                if (
                    stop_ts is not None
                    and (now.ts - stop_ts).total_seconds()
                    < params.counter_stop_cooldown_minutes * 60
                ):
                    continue
            last_created[template] = now.ts
            open_setups.append(
                OpenSetup(
                    template=template,
                    direction=candidate.direction,
                    entry=candidate.entry,
                    target=candidate.target,
                    stop=candidate.stop,
                    created=now.ts,
                    counter=counter,
                )
            )
            open_templates.add(template)

    for item in open_setups:  # neuzavřené do konce dne = bez výsledku
        done.append(
            {
                "template": item.template,
                "direction": item.direction.value,
                "created": item.created,
                "closed": None,
                "outcome": "active",
                "r": None,
            }
        )
    return done


def summarize(rows: list[dict]) -> dict:
    closed = [r for r in rows if r["r"] is not None]
    wins = [r for r in closed if r["outcome"] == Outcome.TARGET.value]
    total_r = sum(r["r"] for r in closed)
    return {
        "setupů": len(rows),
        "uzavřených": len(closed),
        "úspěšnost %": round(100 * len(wins) / len(closed), 1) if closed else 0.0,
        "Ø R": round(total_r / len(closed), 2) if closed else 0.0,
        "Σ R": round(total_r, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="ES,NQ")
    parser.add_argument("--db", default=os.environ.get("GEXLENS_DATABASE_URL", ""))
    args = parser.parse_args()

    repo = OIEodRepository(create_engine(args.db)) if args.db else None

    # Produkční default = absolutní prahy (násobky ATR jsou 0, viz #434)
    configs = {
        "baseline (produkční prahy)": SetupParams(),
        "ATR škálované (#434)": SetupParams(wall_zone_atr=1.9, rejection_min_atr=0.6),
    }

    report: dict = {}
    for symbol in args.symbols.split(","):
        expiries = sorted(
            os.path.basename(p)
            for p in glob.glob(f"{DATA}/{symbol}/*")
            if os.path.basename(p).isdigit()
        )
        per_config: dict[str, list[dict]] = {name: [] for name in configs}
        per_template: dict[str, dict[str, list[dict]]] = {
            name: defaultdict(list) for name in configs
        }
        per_day: dict[str, dict[str, float]] = defaultdict(dict)
        for expiry in expiries:
            minutes = build_minutes(symbol, expiry, repo) if repo else []
            if len(minutes) < 60:
                continue
            for name, params in configs.items():
                rows = replay(minutes, params)
                per_config[name].extend(rows)
                for row in rows:
                    per_template[name][row["template"]].append(row)
                closed = [r["r"] for r in rows if r["r"] is not None]
                per_day[expiry][name] = round(sum(closed), 2)
        report[symbol] = {
            "dnů": len(expiries),
            "Σ R po dnech": dict(sorted(per_day.items())),
            "konfigurace": {
                name: {
                    "celkem": summarize(rows),
                    "per šablona": {
                        tmpl: summarize(trows) for tmpl, trows in sorted(per_template[name].items())
                    },
                }
                for name, rows in per_config.items()
            },
        }
    out = os.environ.get("BACKTEST_OUT")
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"zapsáno: {out}".encode("ascii", "replace").decode())
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
