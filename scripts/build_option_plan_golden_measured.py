"""Naměřený golden plánu opce QQQ z uloženého plánu a partic (F6 #1750, Rozhodnuto 19. 11.).

Jen čtení: `GET /option-plans/{id}` a `GET /scenarios` z API a partice `--data`. Píše jen
`engine/tests/golden/option_plan_measured_{den}.json` (nebo `--out`).

Golden zamyká celý den plánu (Q2 A):
- vznik v 9:15 ET (`build_option_plan`, hlavní plán i stíny K10 B) z naměřeného řetězu
  0DTE a 1DTE snímku 9:15 (`snapshots/QQQ/{expirace}/{den}.parquet`);
- přecenění v 9:31 ET (`reprice_variants`) z řetězu 1DTE snímku 9:31;
- vyhodnocení F3 (`evaluate_option_plan`) z 1m barů QQQ a minutových kotací kontraktů;
- živý stav F3b (`evaluate_plan` s `now` po minutách, `live_transitions`).

Očekávaná čísla jsou z uloženého řádku v PG (Q3 A), co produkce spočítala v okamžiku plánu.
Pole závislá na parametrech účtu se vypouštějí (`ACCOUNT_FIELDS`, verdikty variant a třída
rozpočtu), test je ověří se syntetickými parametry. Ceny opce z modelu (SL, TP1, TP2, přísné
R:R, čerpání P4) skript přepočítá nezávisle vlastním Black–Scholes (`_bs`, bez kódu aplikace).
Naměřené P&L výstupů přepočítá přímo z kotací. Shoda s PG je podmínka zápisu.

Okno barů a kotací (Q4): od US openu do posledního rozhodnutí hlavního plánu i stínů, nejpozději
do exitu. Skript ověří, že vyhodnocení z okna dá týž výsledek jako uložený.

Spuštění z kořene repa (API produkce na :8010):
    PYTHONPATH=engine/src python scripts/build_option_plan_golden_measured.py --plan 2 \
        --data "D:/Documents/Visual Studio Code/GEX/data"
"""

import argparse
import asyncio
import dataclasses
import datetime as dt
import io
import json
import math
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from gexlens_engine.compute.dayverdict import AutoScenario, LevelInputs
from gexlens_engine.compute.option_plan import (
    EXIT_BEFORE_SETTLE,
    MAIN_RULES,
    P4_EXTRA_EM,
    SHADOWS,
    AccountParams,
    build_option_plan,
    evaluate_option_plan,
    jsonable,
    plan_to_json,
)
from gexlens_engine.compute.scenario import Bar
from gexlens_engine.compute.settle import session_bounds, settle_ts, us_open_ts
from gexlens_engine.scenario_auto import ApiReader
from gexlens_engine.storage.option_plans_store import effective_plan
from gexlens_engine.storage.parquet_store import read_bars, read_option_quotes

PRODUCT = "QQQ"
#: Syntetické parametry účtu (ne skutečný účet; hodnoty Settings se do goldenu nečtou)
SYNTHETIC_ACCOUNT = {"account_usd": 12_345.0, "max_loss_usd": 150.0}
#: Pole odvozená z parametrů účtu (verdikt při vzniku čte nastavení, K2; třída P4 rozpočet)
ACCOUNT_FIELDS = frozenset({"budget", "loss_at_stop_pct", "drawdown_pct"})
VARIANT_ACCOUNT_FIELDS = frozenset({"verdict", "reasons"})
CHAIN_KEYS = ("strike", "right", "bid", "ask", "iv", "delta", "gamma", "oi")
QUOTE_KEYS = ("ts_min", "expiry", "strike", "right", "bid", "ask", "iv", "quote_age_s")
MULTIPLIER = 100.0
YEAR_S = 365.0 * 86400.0
TAU_FLOOR_S = 300.0
#: Shoda nezávislého přepočtu s PG (Linux × Windows se liší až na ~1e-12)
TOLERANCE = 1e-6


def strip_account(value: Any, *, variant: bool = False) -> Any:
    """Kopie JSON bez polí závislých na účtu; `variant` = položka seznamu `variants`."""
    if isinstance(value, Mapping):
        drop = ACCOUNT_FIELDS | (VARIANT_ACCOUNT_FIELDS if variant else frozenset())
        return {
            key: strip_account(item, variant=key == "variants")
            for key, item in value.items()
            if key not in drop
        }
    if isinstance(value, list):
        return [strip_account(item, variant=variant) for item in value]
    return value


def strip_plan(part: Mapping[str, Any]) -> dict[str, Any]:
    """Část plánu bez verdiktů (verdikt plánu i variant při vzniku čte parametry účtu)."""
    out = strip_account(part)
    out.pop("verdict", None)
    return out


def same(a: Any, b: Any, path: str = "") -> list[str]:
    """Rozdíly dvou JSON; čísla s tolerancí `TOLERANCE`."""
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        out: list[str] = []
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                out.append(f"{path}.{key}: chybí v {'prvním' if key not in a else 'druhém'}")
            else:
                out.extend(same(a[key], b[key], f"{path}.{key}"))
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: délka {len(a)} × {len(b)}"]
        return [
            d for i, (x, y) in enumerate(zip(a, b, strict=True)) for d in same(x, y, f"{path}[{i}]")
        ]
    if isinstance(a, int | float) and isinstance(b, int | float) and not isinstance(a, bool):
        return (
            []
            if math.isclose(a, b, rel_tol=TOLERANCE, abs_tol=TOLERANCE)
            else [f"{path}: {a!r} × {b!r}"]
        )
    return [] if a == b else [f"{path}: {a!r} × {b!r}"]


def require_same(what: str, a: Any, b: Any) -> None:
    diffs = same(a, b)
    if diffs:
        raise SystemExit(f"{what}: neshoda s PG ({len(diffs)}): " + "; ".join(diffs[:8]))


@dataclasses.dataclass(frozen=True)
class ChainRow:
    ts_min: dt.datetime
    strike: float
    right: str
    bid: float | None
    ask: float | None
    iv: float | None
    delta: float | None
    gamma: float | None
    oi: float | None


def chain_rows(path: Path, ts: dt.datetime) -> list[dict[str, Any]]:
    """Řádky snímku řetězu v minutě `ts` (snímek se zapisuje jednou v 9:15 a v 9:31 ET)."""
    rows = [row for row in pq.read_table(path).to_pylist() if row["ts_min"] == ts]
    if not rows:
        raise SystemExit(f"{path}: snímek {ts:%H:%M} UTC chybí")
    return [{key: row[key] for key in CHAIN_KEYS} for row in sorted(rows, key=_chain_key)]


def _chain_key(row: Mapping[str, Any]) -> tuple[float, str]:
    return (float(row["strike"]), str(row["right"]))


def as_chain(rows: Sequence[Mapping[str, Any]], ts: dt.datetime) -> list[ChainRow]:
    return [ChainRow(ts_min=ts, **row) for row in rows]


def _bs(spot: float, strike: float, iv: float, tau_s: float, right: str) -> float:
    """Black–Scholes s r = q = 0, nezávislý na `gexfield.bs_price`."""
    tau = max(tau_s, TAU_FLOOR_S) / YEAR_S
    sd = iv * math.sqrt(tau)
    d1 = (math.log(spot / strike) + 0.5 * sd * sd) / sd
    d2 = d1 - sd

    def n(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    if right == "C":
        return spot * n(d1) - strike * n(d2)
    return strike * n(-d2) - spot * n(-d1)


def _clamp(ts: dt.datetime, low: dt.datetime, high: dt.datetime) -> dt.datetime:
    return min(max(ts, low), high)


def independent(
    part: Mapping[str, Any], result: Mapping[str, Any], quotes: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Ceny opce z modelu a P&L výstupů nezávisle na modulu (Rozhodnuto 19. 11., Q3).

    Vstupy modelu jsou podle Rozhodnuto 16. 11. (R:R z kotace A): IV z kotace minuty spouště,
    cena SL a TP1 v čase TP1 ze scénáře (sevřeném do vstup–exit), TP2 v čase TP2, přísné R:R
    a čerpání P4 (SL + 0,2 EM proti směru) v exitu. Zaplaceno = ask kotace spouště.
    """
    entry = result.get("entry")
    if result.get("status") != "spuštěno" or entry is None:
        return []
    side = 1 if part["direction"] == "long" else -1
    entry_ts = dt.datetime.fromisoformat(entry["ts"])
    exit_ts = dt.datetime.fromisoformat(part["exit_ts"])
    target_ts = [dt.datetime.fromisoformat(ts) for ts in part["target_ts"]]
    targets = [float(t) for t in part["targets"]]
    stop = float(entry["stop"])
    em = float(part["em"])
    by_minute = {(q["ts_min"], q["strike"], q["right"]): q for q in quotes}
    out: list[dict[str, Any]] = []
    for variant in result["variants"]:
        quote = variant.get("entry_quote")
        if quote is None or quote.get("iv") is None or quote.get("ask") is None:
            continue
        strike, right = float(variant["strike"]), str(variant["right"])
        expiry = settle_ts(dt.date.fromisoformat(variant["expiry"]))
        iv, paid = float(quote["iv"]), float(quote["ask"])

        def price(
            spot: float,
            ts: dt.datetime,
            k: float = strike,
            iv: float = iv,
            right: str = right,
            expiry: dt.datetime = expiry,
        ) -> float:
            return _bs(spot, k, iv, (expiry - ts).total_seconds(), right)

        h1 = _clamp(target_ts[0], entry_ts, exit_ts)
        at_stop = price(stop, h1)
        at_tp1 = price(targets[0], h1)
        at_tp2 = (
            price(targets[1], _clamp(target_ts[1], entry_ts, exit_ts)) if len(targets) > 1 else None
        )
        strict = price(stop, exit_ts)
        worst = price(stop - side * P4_EXTRA_EM * em, exit_ts)
        legs: dict[str, Any] = {}
        for leg in ("exit_tp1", "exit_hold"):
            ex = variant.get(leg)
            if ex is None or ex.get("bid") is None:
                continue
            minute = (dt.datetime.fromisoformat(ex["ts"]) - dt.timedelta(minutes=1)).isoformat()
            row = by_minute.get((minute, strike, right))
            if row is None or row["bid"] != ex["bid"]:
                raise SystemExit(f"{leg} {strike:g}{right}: kotace minuty {minute} nesedí s PG")
            legs[leg] = {
                "event": ex["event"],
                "ts": ex["ts"],
                "bid": row["bid"],
                "pnl_usd": (row["bid"] - paid) * MULTIPLIER,
            }
        entry_minute = (entry_ts - dt.timedelta(minutes=1)).isoformat()
        row = by_minute.get((entry_minute, strike, right))
        if row is None or row["ask"] != paid:
            raise SystemExit(f"vstup {strike:g}{right}: kotace minuty {entry_minute} nesedí s PG")
        out.append(
            {
                "role": variant["role"],
                "strike": strike,
                "right": right,
                "iv": iv,
                "paid": paid,
                "at_stop": at_stop,
                "at_tp1": at_tp1,
                "at_tp2": at_tp2,
                "rr": (at_tp1 - paid) / (paid - at_stop) if paid > at_stop else None,
                "rr_strict": (at_tp1 - paid) / (paid - strict) if paid > strict else None,
                "loss_at_stop_usd": (at_stop - paid) * MULTIPLIER,
                "drawdown_usd": (worst - paid) * MULTIPLIER,
                "pnl_tp1_usd": (at_tp1 - paid) * MULTIPLIER,
                "pnl_tp2_usd": (at_tp2 - paid) * MULTIPLIER if at_tp2 is not None else None,
                **legs,
            }
        )
    return out


def check_independent(
    name: str, ours: Sequence[Mapping[str, Any]], result: Mapping[str, Any]
) -> None:
    stored = {(v["role"], float(v["strike"])): v for v in result["variants"]}
    for item in ours:
        variant = stored[(item["role"], item["strike"])]
        pricing = variant["pricing"]
        for key in (
            "paid",
            "at_stop",
            "at_tp1",
            "at_tp2",
            "rr",
            "rr_strict",
            "loss_at_stop_usd",
            "drawdown_usd",
            "pnl_tp1_usd",
            "pnl_tp2_usd",
        ):
            require_same(f"{name} {item['role']} {key} (nezávislý model)", item[key], pricing[key])
        for leg in ("exit_tp1", "exit_hold"):
            if leg in item:
                require_same(
                    f"{name} {item['role']} {leg} P&L z kotací",
                    item[leg]["pnl_usd"],
                    variant[leg]["pnl_usd"],
                )


def decision_ts(part: Mapping[str, Any], result: Mapping[str, Any]) -> dt.datetime:
    """Okamžik rozhodnutí části: poslední událost s výsledkem, jinak exit (bez vstupu, mezera)."""
    exit_ts = dt.datetime.fromisoformat(part["exit_ts"])
    if (
        result.get("status") == "spuštěno"
        and result.get("outcome") is not None
        and result["events"]
    ):
        return dt.datetime.fromisoformat(result["events"][-1]["ts"])
    return exit_ts


async def fetch(api_url: str, plan_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    api = ApiReader(api_url, timeout_s=60.0)
    try:
        row = await api.get(f"/option-plans/{plan_id}")
        scenarios = (await api.get("/scenarios", {"limit": 500, "source": "auto"}))["scenarios"]
    finally:
        await api.close()
    row = row.get("option_plan", row)
    scenario = next((s for s in scenarios if int(s["id"]) == int(row["scenario_id"])), None)
    if scenario is None:
        raise SystemExit(f"scénář #{row['scenario_id']} plánu #{plan_id} v GET /scenarios není")
    return row, scenario


def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=int, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--api", default="http://localhost:8010")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    data: Path = args.data
    row, scenario = asyncio.run(fetch(args.api, args.plan))
    day = dt.date.fromisoformat(row["day"])
    plan = row["plan"]
    if plan.get("main") is None or (plan.get("repricing") or {}).get("plan") is not None:
        raise SystemExit(
            "plán sestavený až v 9:31 ET (bez opce v 9:15) skript zatím nezná — doplní F6b #1859"
        )
    if not plan["repricing"].get("prices"):
        raise SystemExit(
            f"plán #{args.plan}: přecenění 9:31 bez cen ({plan['repricing'].get('reasons')})"
        )
    if row.get("result") is None:
        raise SystemExit(f"plán #{args.plan} ještě není vyhodnocený (F3 po close)")
    main_part = plan["main"]
    chain = plan["chain"]
    repricing_chain = plan["repricing"]["chain"]
    ts_915 = dt.datetime.fromisoformat(main_part["created_at"])
    ts_931 = dt.datetime.fromisoformat(repricing_chain["ts"]).replace(second=0, microsecond=0)
    exp0, exp1 = chain["zero_dte"]["expiry"], chain["one_dte"]["expiry"]
    if repricing_chain["one_dte"]["expiry"] != exp1:
        raise SystemExit("přecenění: jiná expirace 1DTE než v 9:15")
    snap = data / "snapshots" / PRODUCT
    chain0 = chain_rows(snap / exp0 / f"{day}.parquet", ts_915)
    chain1 = chain_rows(snap / exp1 / f"{day}.parquet", ts_915)
    chain1_931 = chain_rows(snap / exp1 / f"{day}.parquet", ts_931)
    if (len(chain0), len(chain1)) != (chain["zero_dte"]["rows"], chain["one_dte"]["rows"]):
        raise SystemExit("snímek 9:15: počet řádků partice nesedí s plánem")
    if len(chain1_931) != repricing_chain["one_dte"]["rows"]:
        raise SystemExit("snímek 9:31: počet řádků 1DTE partice nesedí s plánem")

    rationale = scenario["rationale"]
    auto = AutoScenario(
        direction=rationale["verdict"],
        entry=float(scenario["entry"]),
        targets=tuple(float(t) for t in scenario["targets"]),
        target_labels=tuple(str(label) for label in rationale.get("targets") or ()),
        path=tuple(
            (dt.datetime.fromisoformat(str(p["ts"])), float(p["price"])) for p in scenario["path"]
        ),
    )
    levels = LevelInputs(**plan["levels_nq"])
    account = AccountParams(**SYNTHETIC_ACCOUNT)
    expiry1 = dt.datetime.strptime(exp1, "%Y%m%d").date()

    def build(rules: Any) -> dict[str, Any]:
        return plan_to_json(
            build_option_plan(
                scenario=auto,
                levels=levels,
                ratio=chain["ratio"],
                spot=chain["spot"],
                chain_0dte=as_chain(chain0, ts_915),
                chain_1dte=as_chain(chain1, ts_915),
                expiry_1dte=expiry1,
                account=account,
                now=ts_915,
                rules=rules,
            )
        )

    built = {"main": build(MAIN_RULES), **{key: build(rules) for key, rules in SHADOWS.items()}}
    stored_plan = {"main": main_part, **plan["shadows"]}
    expected_plan = {key: strip_plan(part) for key, part in stored_plan.items()}
    for key, part in built.items():
        require_same(f"vznik {key}", strip_plan(part), expected_plan[key])

    eff = effective_plan(plan)
    result = row["result"]
    parts = {"main": eff["main"], **eff["shadows"]}
    results = {"main": result["main"], **result["shadows"]}
    decision = max(decision_ts(parts[key], results[key]) for key in parts)
    decision = min(decision, settle_ts(day) - EXIT_BEFORE_SETTLE)
    start, _ = session_bounds(day)
    open_ts = us_open_ts(day)
    all_bars = read_bars(data / "derived", PRODUCT, start, settle_ts(day))
    all_quotes = read_option_quotes(data / "derived", PRODUCT, day)
    bars = [b for b in all_bars if open_ts <= b.ts < decision]
    quotes = [q for q in all_quotes if open_ts <= q.ts_min < decision]
    reasons = [*eff["reasons"], *(eff["repricing_reasons"] or [])]
    window = evaluate_option_plan(
        eff["main"],
        eff["shadows"],
        reasons=reasons,
        bars=[Bar(b.ts, b.high, b.low, b.close) for b in bars],
        quotes=quotes,
        max_loss_usd=SYNTHETIC_ACCOUNT["max_loss_usd"],
    )
    require_same("vyhodnocení z okna", strip_account(window), strip_account(result))

    quote_rows = [jsonable({key: getattr(q, key) for key in QUOTE_KEYS}, "kotace") for q in quotes]
    checks: dict[str, list[dict[str, Any]]] = {}
    for key in parts:
        ours = independent(parts[key], results[key], quote_rows)
        check_independent(key, ours, results[key])
        checks[key] = ours

    live_alerts = row.get("live_alerts") or []
    out = {
        "description": (
            f"Naměřený golden plánu opce QQQ #{args.plan} z {day.day}. {day.month}. {day.year} "
            "(F6 #1750, Rozhodnuto 19. 11.): vznik v 9:15 ET, přecenění v 9:31 ET, vyhodnocení F3 "
            "a živý stav F3b. Vstupy jsou z produkčních partic a GET API, vygenerované skriptem "
            "scripts/build_option_plan_golden_measured.py. Očekávaná čísla jsou z uloženého řádku "
            "plánu v PG bez polí závislých na účtu, model ceny opce ověřený nezávislým "
            "Black–Scholes."
        ),
        "readme": [
            "Řetěz 0DTE a 1DTE v 9:15 ET a 1DTE v 9:31 ET je naměřený (tastytrade, partice "
            "snapshots/QQQ), řádky jsou celé snímky, ze kterých plán vznikl a přecenil se.",
            "Poměr NQ/QQQ, spot QQQ a úrovně NQ jsou ze snímku a plánu v okamžiku vzniku "
            "(plan.chain, plan.levels_nq). Scénář NQ je z GET /scenarios.",
            "Bary QQQ [ts, high, low, close] a kotace kontraktů plánu (sloupce `quote_columns`) "
            "jsou od US openu do posledního rozhodnutí hlavního plánu i stínů, nejpozději do exitu "
            "(Rozhodnuto 19. 11., Q4). Vyhodnocení z okna se shoduje s uloženým.",
            "Parametry účtu jsou syntetické (`synthetic_account`). Verdikty variant a plánu při "
            "vzniku a třída rozpočtu P4 z nich vycházejí, proto v `expected` nejsou.",
            "`independent`: ceny opce z modelu (SL, TP1, TP2, přísné R:R, čerpání P4) spočítané "
            "vlastním Black–Scholes skriptu, IV z kotace minuty spouště a P&L výstupů = "
            "(bid − ask) × 100 přímo z kotací. Všechno se shoduje s PG na 1e-6.",
            "`live_alerts`: přechody odeslané F3b (událost, čas, konečný), prázdné = F3b v den "
            "plánu ještě neběžel.",
        ],
        "plan_id": args.plan,
        "day": day.isoformat(),
        "scenario": {
            "id": int(scenario["id"]),
            "direction": auto.direction,
            "entry": auto.entry,
            "targets": list(auto.targets),
            "target_labels": list(auto.target_labels),
            "path": [[ts.isoformat(), price] for ts, price in auto.path],
        },
        "levels_nq": plan["levels_nq"],
        "synthetic_account": SYNTHETIC_ACCOUNT,
        "slot_915": {
            "ts": ts_915.isoformat(),
            "ratio": chain["ratio"],
            "spot": chain["spot"],
            "expiry_0dte": exp0,
            "expiry_1dte": exp1,
            "chain_0dte": chain0,
            "chain_1dte": chain1,
        },
        "slot_931": {"ts": ts_931.isoformat(), "chain_1dte": chain1_931},
        "window": {"start": open_ts.isoformat(), "end": decision.isoformat()},
        "bars_qqq": [[b.ts.isoformat(), b.high, b.low, b.close] for b in bars],
        "quote_columns": list(QUOTE_KEYS),
        "quotes": [[q[key] for key in QUOTE_KEYS] for q in quote_rows],
        "live_alerts": [
            {key: item[key] for key in ("event", "ts", "final")} for item in live_alerts
        ],
        "expected": {
            "plan": expected_plan,
            "repricing_prices": plan["repricing"]["prices"],
            "result": strip_account(result),
            "independent": checks,
        },
    }
    path = args.out or Path(f"engine/tests/golden/option_plan_measured_{day:%Y%m%d}.json")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(
        f"{path}: plán #{args.plan} {day}, 0DTE {len(chain0)},"
        f" 1DTE {len(chain1)} + {len(chain1_931)},"
        f" okno {open_ts:%H:%M}–{decision:%H:%M} UTC, barů {len(bars)}, kotací {len(quote_rows)},"
        f" živých přechodů {len(live_alerts)}, {path.stat().st_size / 1024:.0f} KB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
