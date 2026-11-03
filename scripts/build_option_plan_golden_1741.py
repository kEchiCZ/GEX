"""Golden dataset plánu opce QQQ 2. 11. 2026 (#1741, ADR-0062 K9 A) z produkčních dat.

Jen čtení: partice `--data` a GET API (`/scenarios`, `scenario_auto.gather_context`). Píše jen
`engine/tests/golden/option_plan_1741.json` (nebo `--out`).

Vstupy plánu v 9:31 ET (K4 A):
- scénář #29 z `GET /scenarios`;
- úrovně NQ 0DTE ze snímku `levels` 14:31 UTC;
- ONH/ONL/PDH/PDL/PDC z `gather_context`, tedy tatáž funkce jako scénář;
- poměr NQ/QQQ z posledního páru 1m barů před 9:31 ET (14:25 UTC, bar QQQ 9:30 ET chybí);
- spot NQ = close baru 14:30 UTC;
- řetěz QQQ 0DTE = poslední kotace každého kontraktu za 10 min (9:23–9:25 ET), pásmo ±3 %;
- bary NQ v RTH (bary QQQ v RTH se 2. 11. nesbíraly, test je dělí poměrem).

Řetěz QQQ 1DTE se 2. 11. nesbíral. Fixture je **rekonstrukce** (viz `RECONSTRUCTION`):
Black–Scholes s jednou IV 30 %, se kterou ADR-0062 (Kontext) ověřil model proti naměřeným
bodům putu 687, a se spreadem 0,02 jako naměřená kotace 2,68/2,70. Naměřený dataset doplní
F6 #1750.

Očekávaná čísla počítají nezávisle funkce skriptu přehrání
`scripts/measure_option_plan_entries_1738.py` (#1756), ne testovaný modul.

Spuštění z kořene repa (API produkce na :8010):
    PYTHONPATH=engine/src python scripts/build_option_plan_golden_1741.py \
        --data "D:/Documents/Visual Studio Code/GEX/data"
"""

import argparse
import asyncio
import datetime as dt
import io
import json
import math
import sys
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))

import measure_option_plan_entries_1738 as replay  # noqa: E402

from gexlens_engine.compute.gexfield import bs_price, fallback_greeks  # noqa: E402
from gexlens_engine.compute.settle import settle_ts, us_open_ts  # noqa: E402
from gexlens_engine.scenario_auto import ApiReader, gather_context  # noqa: E402

DAY = dt.date(2026, 11, 2)
EXPIRY = dt.date(2026, 11, 3)
SCENARIO_ID = 29
PLAN_TS = us_open_ts(DAY) + dt.timedelta(minutes=1)  # 9:31 ET
RATIO_TS = dt.datetime(2026, 11, 2, 14, 25, tzinfo=dt.UTC)
SPOT_TS = us_open_ts(DAY)  # bar 9:30 ET, uzavřený v 9:31 ET
BAND = 0.03  # ±3 % kolem spotu (ADR-0062 Odvozeno)
LOOKBACK = dt.timedelta(minutes=10)
FIXTURE_IV = 0.30
FIXTURE_HALF_SPREAD = 0.01
RECONSTRUCTION = [
    "Řetěz QQQ 1DTE (expirace 3. 11.) se 2. 11. nesbíral, tato fixture je REKONSTRUKCE.",
    "Kontrakty: striky po 1 $ v pásmu ±3 % kolem spotu 9:31 ET, call i put.",
    "Mid = Black–Scholes (r = q = 0) se spotem 9:31 ET, τ do settle 3. 11. a jednou IV 30 %.",
    "IV 30 % ověřila ADR-0062 (Kontext) proti naměřeným bodům putu 687: fill vlastníka 1,40 $ "
    "v 16:50 SEČ → model 1,42 (+1 %), kotace 2,68/2,70 v 18:31 SEČ → model 2,58 (−4 %). "
    "IV z těchto bodů vychází 29,8–30,6 %.",
    "Bid/ask = mid ∓ 0,01 zaokrouhlené na centy (spread 0,02 jako kotace 2,68/2,70); kontrakty "
    "s bid ≤ 0 vynechané. Delta a gamma z téže IV (`gexfield.fallback_greeks`), OI nenaměřené "
    "(null).",
    "AC #1741 počítá put 687 s IV 31,4 % = ATM IV NQ 1DTE v 9:31 ET (`iv_nq_1dte_atm`), jako "
    "zpětné přehrání #1756. Test ho ověřuje touto IV nad týmiž kontrakty.",
    "Naměřený dataset 1DTE doplní F6 #1750.",
]


def read_rows(path: Path, start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
    filters = [("ts_min", ">=", start), ("ts_min", "<=", end)]
    rows: list[dict[str, Any]] = pq.read_table(path, filters=filters).to_pylist()
    return rows


def one(rows: list[dict[str, Any]], ts: dt.datetime, what: str) -> dict[str, Any]:
    found = [row for row in rows if row["ts_min"] == ts]
    if len(found) != 1:
        raise SystemExit(f"{what}: v {ts:%H:%M} UTC čekám 1 řádek, je {len(found)}")
    return found[0]


def latest_chain(path: Path, ts: dt.datetime) -> dict[tuple[float, str], dict[str, Any]]:
    latest: dict[tuple[float, str], dict[str, Any]] = {}
    for row in sorted(read_rows(path, ts - LOOKBACK, ts), key=lambda r: r["ts_min"]):
        latest[(float(row["strike"]), str(row["right"]))] = row
    return latest


def chain_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts_min": row["ts_min"].isoformat(),
        **{
            key: row[key] for key in ("strike", "right", "bid", "ask", "iv", "delta", "gamma", "oi")
        },
    }


def reconstruct_1dte(spot: float) -> list[dict[str, Any]]:
    expiry = settle_ts(EXPIRY)
    tau = (expiry - PLAN_TS).total_seconds() / (365 * 86400)
    rows: list[dict[str, Any]] = []
    for strike in range(math.ceil(spot * (1 - BAND)), math.floor(spot * (1 + BAND)) + 1):
        for right in ("C", "P"):
            mid = bs_price(spot, float(strike), FIXTURE_IV, tau, right)
            bid = round(mid - FIXTURE_HALF_SPREAD, 2)
            if bid <= 0:
                continue
            greeks = fallback_greeks(
                spot=spot, strike=float(strike), right=right, mid=mid, settle=expiry, now=PLAN_TS
            )
            if greeks is None:
                raise SystemExit(f"fixture 1DTE: greeks {strike} {right} nevyšly")
            rows.append(
                {
                    "ts_min": PLAN_TS.isoformat(),
                    "strike": float(strike),
                    "right": right,
                    "bid": bid,
                    "ask": round(mid + FIXTURE_HALF_SPREAD, 2),
                    "iv": FIXTURE_IV,
                    "delta": greeks.delta,
                    "gamma": greeks.gamma,
                    "oi": None,
                }
            )
    return rows


def expected(
    scenario: dict[str, Any],
    levels: dict[str, float],
    ratio: float,
    spot: float,
    em: float,
    bars: list[dict[str, Any]],
    iv: float,
) -> dict[str, Any]:
    """Čísla plánu funkcemi přehrání #1756 (hlavní pravidla, stín B a stín knotu)."""
    names = {
        "flip": "flip",
        "call_wall": "call wall",
        "put_wall": "put wall",
        "on_high": "ONH",
        "on_low": "ONL",
        "prev_high": "PDH",
        "prev_low": "PDL",
    }
    option = replay.Option("P", 687.0, iv, settle_ts(EXPIRY))
    plan = replay.Plan(
        scenario=SCENARIO_ID,
        day=DAY,
        side=-1,
        entry=scenario["entry"] / ratio,
        targets=tuple(t / ratio for t in scenario["targets"]),
        t1=dt.datetime.fromisoformat(scenario["path"][1]["ts"]),
        t2=dt.datetime.fromisoformat(scenario["path"][-1]["ts"]),
        levels={names[key]: value / ratio for key, value in levels.items() if key in names},
        em=em,
        option=option,
        fives=replay.five_minute(bars, ratio, us_open_ts(DAY), settle_ts(DAY)),
        exit_ts=settle_ts(DAY) - replay.EXIT_BEFORE_SETTLE,
        p1="blokuje",
        sources={},
    )
    out: dict[str, Any] = {"iv": iv}
    rules = {
        "hlavní": replay.Rules("jen A", short_b=False, long_b=False),
        "stín vstup B": replay.RULES,
        "stín test knotem": replay.Rules("knot", test="wick", short_b=False, long_b=False),
    }
    for name, rule in rules.items():
        result = replay.run(plan, rule)
        if result.chosen is None or result.risk is None:
            raise SystemExit(f"{name}: vstup nespuštěn")
        entry, risk = result.chosen, result.risk
        out[name] = {
            "kind": entry.kind,
            "ts": entry.bar.end.isoformat(),
            "price": round(entry.bar.close, 4),
            "stop": round(entry.stop, 4),
            "paid": round(risk.paid, 4),
            "at_stop": round(risk.at_stop, 4),
            "at_tp1": round(risk.at_tp1, 4),
            "rr": round(risk.rr, 4),
            "rr_strict": round(risk.rr_strict, 4),
            "drawdown_usd": round(risk.p4, 4),
            "trigger_b": (
                {
                    "ts": result.b.bar.end.isoformat(),
                    "price": round(result.b.bar.close, 4),
                    "stop": round(result.b.stop, 4),
                }
                if result.b is not None and rule.short_b
                else None
            ),
        }
    return out


async def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--api", default="http://localhost:8010")
    parser.add_argument(
        "--out", type=Path, default=Path("engine/tests/golden/option_plan_1741.json")
    )
    args = parser.parse_args()
    data: Path = args.data
    api = ApiReader(args.api, timeout_s=60.0)
    try:
        scenarios = (await api.get("/scenarios", {"limit": 500, "source": "auto"}))["scenarios"]
        ctx = await gather_context(api, "NQ", None, DAY, PLAN_TS)
    finally:
        await api.close()
    scenario = next(s for s in scenarios if int(s["id"]) == SCENARIO_ID)
    if ctx.failures:
        raise SystemExit(f"gather_context selhal: {ctx.failures}")
    nq_bars = data / f"derived/NQ/bars/{DAY.isoformat()}.parquet"
    qqq_bars = data / f"derived/QQQ/bars/{DAY.isoformat()}.parquet"
    pair_nq = one(read_rows(nq_bars, RATIO_TS, RATIO_TS), RATIO_TS, "bar NQ")["close"]
    pair_qqq = one(read_rows(qqq_bars, RATIO_TS, RATIO_TS), RATIO_TS, "bar QQQ")["close"]
    later_qqq = read_rows(qqq_bars, RATIO_TS + dt.timedelta(minutes=1), PLAN_TS)
    if later_qqq:
        raise SystemExit(
            f"poměr: bar QQQ po 14:25 UTC existuje ({len(later_qqq)}), pár není poslední"
        )
    ratio = pair_nq / pair_qqq
    spot_nq = one(read_rows(nq_bars, SPOT_TS, SPOT_TS), SPOT_TS, "bar NQ 9:30 ET")["close"]
    spot = spot_nq / ratio
    snap = one(
        read_rows(
            data / f"derived/NQ/{DAY:%Y%m%d}/levels/{DAY.isoformat()}.parquet", PLAN_TS, PLAN_TS
        ),
        PLAN_TS,
        "levels NQ",
    )
    levels = {
        "flip": snap["flip"],
        "call_wall": snap["call_wall"],
        "put_wall": snap["put_wall"],
        "centroid": snap["centroid"],
        "on_high": ctx.on_high,
        "on_low": ctx.on_low,
        "prev_high": ctx.prev_high,
        "prev_low": ctx.prev_low,
        "prev_close": ctx.prev_close,
    }
    chain0 = latest_chain(data / f"snapshots/QQQ/{DAY:%Y%m%d}/{DAY.isoformat()}.parquet", PLAN_TS)
    band0 = [
        chain_row(row)
        for (strike, _), row in sorted(chain0.items())
        if abs(strike - spot) <= BAND * spot
    ]
    nq_1dte = latest_chain(
        data / f"snapshots/NQ/{EXPIRY:%Y%m%d}/{DAY.isoformat()}.parquet", PLAN_TS
    )
    iv_nq = replay.atm_iv(nq_1dte, spot_nq)
    if iv_nq is None:
        raise SystemExit("ATM IV NQ 1DTE chybí")
    rth = read_rows(nq_bars, us_open_ts(DAY), settle_ts(DAY) - dt.timedelta(minutes=1))
    _, em = replay.chain_em(chain0, spot)
    out = {
        "description": (
            "Golden plánu opce QQQ 2. 11. 2026 (#1741, ADR-0062 K9 A): plán v 9:31 ET ze scénáře "
            "NQ #29 (short). Vstupy jsou z produkčních partic a GET API, vygenerované skriptem "
            "scripts/build_option_plan_golden_1741.py. Bary NQ v RTH, test je dělí poměrem (bary "
            "QQQ v RTH se 2. 11. nesbíraly). Očekávaná čísla spočítaly funkce zpětného přehrání "
            "#1756 (scripts/measure_option_plan_entries_1738.py), ne testovaný modul."
        ),
        "readme_1dte": RECONSTRUCTION,
        "plan_ts": PLAN_TS.isoformat(),
        "expiry_1dte": EXPIRY.isoformat(),
        "scenario": {
            "id": SCENARIO_ID,
            "direction": scenario["rationale"]["verdict"],
            "entry": scenario["entry"],
            "targets": scenario["targets"],
            "target_labels": scenario["rationale"]["targets"],
            "path": [[point["ts"], point["price"]] for point in scenario["path"]],
        },
        "levels_nq": levels,
        "ratio_pair": {"ts": RATIO_TS.isoformat(), "nq": pair_nq, "qqq": pair_qqq},
        "spot_nq": spot_nq,
        "iv_nq_1dte_atm": iv_nq,
        "chain_0dte": band0,
        "chain_1dte": reconstruct_1dte(spot),
        "bars_nq": [[r["ts_min"].isoformat(), r["high"], r["low"], r["close"]] for r in rth],
        "expected": {
            "fixture_iv": expected(scenario, levels, ratio, spot, em, rth, FIXTURE_IV),
            "ac_iv_nq": expected(scenario, levels, ratio, spot, em, rth, iv_nq),
        },
    }
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(
        f"{args.out}: poměr {ratio:.4f}, spot {spot:.2f}, EM {em:.2f}, IV NQ {iv_nq:.4f}, "
        f"0DTE {len(band0)} kontraktů, 1DTE {len(out['chain_1dte'])}, barů {len(rth)}"
    )
    print(json.dumps(out["expected"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
