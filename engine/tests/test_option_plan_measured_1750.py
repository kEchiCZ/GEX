"""Plán opce QQQ na naměřených datech (F6 #1750, Rozhodnuto 19. 11.): golden celého dne.

Každý `golden/option_plan_measured_*.json` (`scripts/build_option_plan_golden_measured.py`)
je jeden uložený plán z produkce: naměřený řetěz 0DTE a 1DTE, bary QQQ a kotace kontraktů.
Golden zamyká vznik v 9:15 ET, přecenění v 9:31 ET, vyhodnocení F3 a živý stav F3b proti
uloženému řádku v PG (bez polí závislých na účtu) a model ceny opce proti nezávislému
Black–Scholes skriptu. Parametry účtu jsou syntetické.
"""

import dataclasses
import datetime as dt
import functools
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from gexlens_engine.compute.dayverdict import AutoScenario, LevelInputs
from gexlens_engine.compute.option_plan import (
    MAIN_RULES,
    SHADOWS,
    AccountParams,
    build_option_plan,
    evaluate_option_plan,
    evaluate_plan,
    jsonable,
    live_transitions,
    plan_to_json,
    reprice_variants,
)
from gexlens_engine.compute.scenario import Bar
from gexlens_engine.option_plan_job import variant_keys

GOLDEN = Path(__file__).parent / "golden"
DATASETS = sorted(GOLDEN.glob("option_plan_measured_*.json"))
#: Pole odvozená z parametrů účtu; verdikty variant a plánu při vzniku čtou nastavení (K2)
ACCOUNT_FIELDS = frozenset({"budget", "loss_at_stop_pct", "drawdown_pct"})
VARIANT_ACCOUNT_FIELDS = frozenset({"verdict", "reasons"})
#: PG počítá v Linuxu, lokální běh ve Windows: rozdíl až ~1e-12
TOLERANCE = 1e-9
MINUTE = dt.timedelta(minutes=1)


@dataclasses.dataclass(frozen=True)
class Row:
    ts_min: dt.datetime
    strike: float
    right: str
    bid: float | None
    ask: float | None
    iv: float | None
    delta: float | None
    gamma: float | None
    oi: float | None


@dataclasses.dataclass(frozen=True)
class Quote:
    ts_min: dt.datetime
    expiry: str
    strike: float
    right: str
    bid: float | None
    ask: float | None
    iv: float | None
    quote_age_s: float | None


@dataclasses.dataclass(frozen=True)
class Day:
    raw: dict[str, Any]
    scenario: AutoScenario
    levels: LevelInputs
    account: AccountParams
    ts_915: dt.datetime
    ts_931: dt.datetime
    chain_0dte: list[Row]
    chain_1dte: list[Row]
    chain_1dte_931: list[Row]
    bars: list[Bar]
    quotes: list[Quote]

    @property
    def expected(self) -> dict[str, Any]:
        expected: dict[str, Any] = self.raw["expected"]
        return expected


def _rows(raw: list[dict[str, Any]], ts: str) -> list[Row]:
    return [Row(ts_min=dt.datetime.fromisoformat(ts), **row) for row in raw]


@functools.cache
def _day(path: Path) -> Day:
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    scen = raw["scenario"]
    s915, s931 = raw["slot_915"], raw["slot_931"]
    columns = raw["quote_columns"]
    quotes = []
    for values in raw["quotes"]:
        item = dict(zip(columns, values, strict=True))
        quotes.append(Quote(**{**item, "ts_min": dt.datetime.fromisoformat(item["ts_min"])}))
    return Day(
        raw=raw,
        scenario=AutoScenario(
            direction=scen["direction"],
            entry=scen["entry"],
            targets=tuple(scen["targets"]),
            target_labels=tuple(scen["target_labels"]),
            path=tuple((dt.datetime.fromisoformat(ts), price) for ts, price in scen["path"]),
        ),
        levels=LevelInputs(**raw["levels_nq"]),
        account=AccountParams(**raw["synthetic_account"]),
        ts_915=dt.datetime.fromisoformat(s915["ts"]),
        ts_931=dt.datetime.fromisoformat(s931["ts"]),
        chain_0dte=_rows(s915["chain_0dte"], s915["ts"]),
        chain_1dte=_rows(s915["chain_1dte"], s915["ts"]),
        chain_1dte_931=_rows(s931["chain_1dte"], s931["ts"]),
        bars=[Bar(dt.datetime.fromisoformat(ts), h, lo, c) for ts, h, lo, c in raw["bars_qqq"]],
        quotes=quotes,
    )


@functools.cache
def _built(path: Path) -> dict[str, dict[str, Any]]:
    """Plán v 9:15 ET (hlavní a stíny) jako `plan_to_json`, se syntetickým účtem."""
    day = _day(path)
    s915 = day.raw["slot_915"]

    def build(rules: Any) -> dict[str, Any]:
        return plan_to_json(
            build_option_plan(
                scenario=day.scenario,
                levels=day.levels,
                ratio=s915["ratio"],
                spot=s915["spot"],
                chain_0dte=day.chain_0dte,
                chain_1dte=day.chain_1dte,
                expiry_1dte=dt.datetime.strptime(s915["expiry_1dte"], "%Y%m%d").date(),
                account=day.account,
                now=day.ts_915,
                rules=rules,
            )
        )

    return {"main": build(MAIN_RULES), **{key: build(rules) for key, rules in SHADOWS.items()}}


def _strip(value: Any, *, variant: bool = False) -> Any:
    if isinstance(value, Mapping):
        drop = ACCOUNT_FIELDS | (VARIANT_ACCOUNT_FIELDS if variant else frozenset())
        return {
            key: _strip(item, variant=key == "variants")
            for key, item in value.items()
            if key not in drop
        }
    if isinstance(value, list):
        return [_strip(item, variant=variant) for item in value]
    return value


def _diffs(a: Any, b: Any, path: str = "") -> list[str]:
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        if set(a) != set(b):
            return [f"{path}: klíče {sorted(set(a) ^ set(b))}"]
        return [d for key in sorted(a) for d in _diffs(a[key], b[key], f"{path}.{key}")]
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: délka {len(a)} × {len(b)}"]
        return [
            d
            for i, (x, y) in enumerate(zip(a, b, strict=True))
            for d in _diffs(x, y, f"{path}[{i}]")
        ]
    numbers = isinstance(a, int | float) and isinstance(b, int | float)
    if numbers and not isinstance(a, bool) and not isinstance(b, bool):
        close = math.isclose(a, b, rel_tol=TOLERANCE, abs_tol=TOLERANCE)
        return [] if close else [f"{path}: {a!r} × {b!r}"]
    return [] if a == b else [f"{path}: {a!r} × {b!r}"]


def _evaluated(path: Path) -> dict[str, Any]:
    day = _day(path)
    built = _built(path)
    return evaluate_option_plan(
        built["main"],
        {key: built[key] for key in SHADOWS},
        reasons=[],
        bars=day.bars,
        quotes=day.quotes,
        max_loss_usd=day.account.max_loss_usd,
    )


measured = pytest.mark.parametrize("path", DATASETS, ids=[p.stem for p in DATASETS])


def test_datasets_present() -> None:
    """Naměřený den 16. 11. (F6 #1750) je v sadě; další přidá F6b #1859 skriptem."""
    assert "option_plan_measured_20261116" in [p.stem for p in DATASETS]


@measured
def test_synthetic_account(path: Path) -> None:
    """Parametry účtu goldenu jsou syntetické, ne hodnoty ze Settings."""
    assert _day(path).raw["synthetic_account"] == {"account_usd": 12_345.0, "max_loss_usd": 150.0}


@measured
def test_plan_at_creation(path: Path) -> None:
    """Vznik v 9:15 ET z naměřeného řetězu = uložený plán (hlavní i stíny K10 B)."""
    built = _built(path)
    expected = _day(path).expected["plan"]
    assert set(built) == set(expected)
    for key, part in built.items():
        got = _strip(part)
        got.pop("verdict")
        assert _diffs(got, expected[key]) == [], key


@measured
def test_verdict_waits_for_entry_a(path: Path) -> None:
    """K12 A: s nastaveným (syntetickým) účtem čekají plán i varianty na spoušť vstupu A."""
    main = _built(path)["main"]
    assert main["verdict"].startswith("čeká")
    assert {variant["verdict"] for variant in main["variants"]} == {"čeká"}
    assert all(variant["pricing"] is None for variant in main["variants"])


@measured
def test_repricing_at_931(path: Path) -> None:
    """Přecenění v 9:31 ET řetězem 1DTE téže minuty = uložené ceny (K4 C bod 2)."""
    day = _day(path)
    expiry = dt.datetime.strptime(day.raw["slot_915"]["expiry_1dte"], "%Y%m%d").date()
    expected = day.expected["repricing_prices"]
    for key, part in _built(path).items():
        repriced, gaps = reprice_variants(
            variant_keys(part),
            chain_1dte=day.chain_1dte_931,
            expiry_1dte=expiry,
            targets=[float(target) for target in part["targets"]],
            target_ts=[dt.datetime.fromisoformat(ts) for ts in part["target_ts"]],
            exit_ts=dt.datetime.fromisoformat(part["exit_ts"]),
            now=day.ts_931,
        )
        assert gaps == ()
        assert _diffs(jsonable(list(repriced)), expected[key]) == [], key


@measured
def test_evaluation_from_measured_quotes(path: Path) -> None:
    """Vyhodnocení F3 z naměřených barů a kotací = uložený výsledek."""
    got = _evaluated(path)
    assert _diffs(_strip(got), _day(path).expected["result"]) == []


@measured
def test_budget_class_with_synthetic_limit(path: Path) -> None:
    """Třída P4 podle syntetického rozpočtu: nad rozpočtem, když čerpání překročí limit."""
    got = _evaluated(path)
    limit = _day(path).account.max_loss_usd
    assert limit is not None
    parts = [got["main"], *got["shadows"].values()]
    for variant in (v for part in parts for v in part["variants"]):
        if variant["pricing"] is None:
            assert variant["budget"] is None
            continue
        over = -variant["pricing"]["drawdown_usd"] > limit
        assert variant["budget"] == ("nad rozpočtem" if over else "v rozpočtu")


@measured
def test_model_matches_independent_black_scholes(path: Path) -> None:
    """Ceny opce z modelu = nezávislý BS skriptu; P&L výstupů = (bid − ask) × 100 z kotací."""
    day = _day(path)
    got = _evaluated(path)
    results = {"main": got["main"], **got["shadows"]}
    quotes = {(q.ts_min, q.strike, q.right): q for q in day.quotes}
    for key, items in day.expected["independent"].items():
        by_variant = {(v["role"], v["strike"]): v for v in results[key]["variants"]}
        priced = {k for k, v in by_variant.items() if v["pricing"] is not None}
        assert {(item["role"], item["strike"]) for item in items} == priced, key
        for item in items:
            variant = by_variant[(item["role"], item["strike"])]
            for field in ("paid", "at_stop", "at_tp1", "at_tp2", "rr", "rr_strict"):
                assert variant["pricing"][field] == pytest.approx(item[field], abs=1e-6), field
            for field in ("loss_at_stop_usd", "drawdown_usd", "pnl_tp1_usd", "pnl_tp2_usd"):
                assert variant["pricing"][field] == pytest.approx(item[field], abs=1e-4), field
            entry_ts = dt.datetime.fromisoformat(variant["entry_quote"]["ts_min"])
            ask = quotes[(entry_ts, item["strike"], item["right"])].ask
            assert ask is not None and variant["pricing"]["paid"] == ask
            for leg in ("exit_tp1", "exit_hold"):
                if leg not in item:
                    continue
                exit_ts = dt.datetime.fromisoformat(variant[leg]["ts"])
                bid = quotes[(exit_ts - MINUTE, item["strike"], item["right"])].bid
                assert bid is not None
                assert variant[leg]["pnl_usd"] == pytest.approx((bid - ask) * 100, abs=1e-6)


@measured
def test_live_state_replays_result(path: Path) -> None:
    """Živý stav F3b minutu po minutě: každý přechod jednou, hned v minutě, kdy nastal.

    Běžel-li F3b v den plánu, přehrání = odeslané `live_alerts` z PG (F6b #1859). Jinak
    (16. 11.) = události večerního výsledku F3 spuštěného plánu, poslední konečná (#1757 bod 5).
    """
    day = _day(path)
    main = _built(path)["main"]
    window = day.raw["window"]
    until = dt.datetime.fromisoformat(window["start"]) + MINUTE
    end = dt.datetime.fromisoformat(window["end"])
    sent: list[tuple[str, str, bool, dt.datetime]] = []
    while until <= end:
        result = evaluate_plan(
            main,
            rules=MAIN_RULES,
            bars=day.bars,
            quotes=day.quotes,
            max_loss_usd=day.account.max_loss_usd,
            now=until,
        )
        seen = {event for event, *_ in sent}
        for transition in live_transitions(result, until):
            if transition.event not in seen:
                sent.append((transition.event, transition.ts.isoformat(), transition.final, until))
        if sent and sent[-1][2]:
            break
        until += MINUTE
    if day.raw["live_alerts"]:
        expected = [(a["event"], a["ts"], a["final"]) for a in day.raw["live_alerts"]]
    else:
        result = day.expected["result"]["main"]
        assert result["status"] == "spuštěno", "bez live_alerts golden zná jen spuštěný plán"
        events = result["events"]
        expected = [
            (e["kind"], dt.datetime.fromisoformat(e["ts"]).isoformat(), i == len(events) - 1)
            for i, e in enumerate(events)
        ]
    assert [(event, ts, final) for event, ts, final, _ in sent] == expected
    assert all(dt.datetime.fromisoformat(ts) == at for _, ts, _, at in sent)
