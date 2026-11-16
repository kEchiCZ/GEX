"""Vyhodnocení plánu opce QQQ (F3 #1748, ADR-0062 K3, K10 B): golden 2. 11. a stavy automatu.

Golden je plán v 9:31 ET z `option_plan_1741.json` se syntetickými kotacemi
(`option_plan_result_1748.json`, readme). Ostatní případy stojí na ručně složeném plánu
(short, úroveň A = flip 600) a barech po 5min blocích, aby každý stav měl vlastní cestu.
"""

import dataclasses
import datetime as dt
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from test_option_plan_golden_1741 import BARS as GOLDEN_BARS
from test_option_plan_golden_1741 import DATA as PLAN_DATA
from test_option_plan_golden_1741 import _plan

from gexlens_engine.compute.gexfield import _YEAR_S, bs_price
from gexlens_engine.compute.option_plan import (
    MAIN_RULES,
    SHADOW_B,
    SHADOW_WICK,
    TRACK_RECORD_SINCE,
    PlanResult,
    evaluate_option_plan,
    evaluate_plan,
    plan_to_json,
    track_record,
)
from gexlens_engine.compute.scenario import Bar
from gexlens_engine.compute.settle import settle_ts

FIXTURE = Path(__file__).parent / "golden" / "option_plan_result_1748.json"
GOLDEN: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
RULE = GOLDEN["quote_rule"]
MINUTE = dt.timedelta(minutes=1)
UTC = dt.UTC


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


def synthetic_quotes(
    bars: Sequence[Bar],
    strikes: Sequence[float],
    expiry: dt.date,
    *,
    right: str = "P",
    iv: float = RULE["iv"],
    bid_factor: float = RULE["bid_factor"],
    ask_factor: float = RULE["ask_factor"],
) -> list[Quote]:
    """Kotace minut podle `quote_rule`: BS z close minuty, čas k závěru minuty."""
    expiry_ts = settle_ts(expiry)
    out: list[Quote] = []
    for bar in bars:
        tau = (expiry_ts - (bar.ts + MINUTE)).total_seconds() / _YEAR_S
        for strike in strikes:
            mid = bs_price(bar.close, strike, iv, tau, right)
            out.append(
                Quote(
                    bar.ts,
                    expiry.strftime("%Y%m%d"),
                    strike,
                    right,
                    bid_factor * mid,
                    ask_factor * mid,
                    iv,
                    RULE["age_s"],
                )
            )
    return out


# ── golden 2. 11. ───────────────────────────────────────────────────

GOLDEN_EXPIRY = dt.date.fromisoformat(PLAN_DATA["expiry_1dte"])
GOLDEN_QUOTES = synthetic_quotes(GOLDEN_BARS, (687.0, 686.0), GOLDEN_EXPIRY)


def _golden(rules: Any = MAIN_RULES, **kwargs: Any) -> PlanResult:
    part = plan_to_json(_plan(bars=(), rules=rules))  # plán při vzniku, bez spouště
    return evaluate_plan(
        part,
        rules=rules,
        bars=kwargs.get("bars", GOLDEN_BARS),
        quotes=kwargs.get("quotes", GOLDEN_QUOTES),
        max_loss_usd=kwargs.get("max_loss_usd", GOLDEN["max_loss_usd"]),
    )


def test_golden_main_path() -> None:
    """AC #1748: A 16:35 SEČ za 697,99, SL 699,66, TP1 v 5min baru 18:30–18:35, TP2 v 18:45."""
    expected = GOLDEN["expected"]["main"]
    result = _golden()
    assert (result.status, result.outcome) == (expected["status"], expected["outcome"])
    entry = result.entry
    assert entry is not None and entry.kind == "A"
    assert entry.ts == dt.datetime.fromisoformat(expected["entry"]["ts"])
    assert (round(entry.price, 2), round(entry.stop, 2)) == (697.99, 699.66)
    assert entry.stop == pytest.approx(expected["entry"]["stop"], abs=1e-4)
    got = [(event.kind, event.ts, event.price) for event in result.events]
    want = [(kind, dt.datetime.fromisoformat(ts), price) for kind, ts, price in expected["events"]]
    assert [g[:2] for g in got] == [w[:2] for w in want]
    assert [g[2] for g in got] == pytest.approx([w[2] for w in want], abs=1e-4)
    tp1 = result.events[1].ts
    assert (
        dt.datetime(2026, 11, 2, 17, 30, tzinfo=UTC)
        < tp1
        <= dt.datetime(2026, 11, 2, 17, 35, tzinfo=UTC)
    )
    assert (result.bars, result.bars_expected) == (expected["bars"], expected["bars"])


def test_golden_variants_from_quotes() -> None:
    """Vstup za ask, výstup za bid v minutě události; R:R z kotace proti přehrání #1756."""
    result = _golden()
    for variant in result.variants:
        want = GOLDEN["expected"]["main"]["variants"][variant.role]
        assert (variant.strike, variant.kind, variant.budget) == (
            want["strike"],
            want["kind"],
            "v rozpočtu",
        )
        assert variant.entry_quote is not None
        assert variant.entry_quote.ts_min == dt.datetime(2026, 11, 2, 15, 34, tzinfo=UTC)
        assert variant.entry_quote.ask == pytest.approx(want["ask"], abs=1e-6)
        assert variant.exit_tp1 is not None and variant.exit_hold is not None
        assert (variant.exit_tp1.event, variant.exit_hold.event) == ("TP1", "TP2")
        assert variant.exit_tp1.bid == pytest.approx(want["bid_tp1"], abs=1e-6)
        assert variant.exit_hold.bid == pytest.approx(want["bid_hold"], abs=1e-6)
        assert variant.exit_tp1.pnl_usd == pytest.approx(want["pnl_tp1_usd"], abs=1e-4)
        assert variant.exit_hold.pnl_usd == pytest.approx(want["pnl_hold_usd"], abs=1e-4)
        assert variant.gaps == ()
    main = result.variants[0]
    assert main.pricing is not None and main.model_label == "model z naměřené IV"
    pricing = main.pricing
    replay = PLAN_DATA["expected"]["fixture_iv"]["hlavní"]
    ask = GOLDEN["expected"]["main"]["variants"]["hlavní"]["ask"]
    assert pricing.paid == pytest.approx(ask, abs=1e-6)  # zaplaceno za ask kotace, ne model
    # Ceny při SL a TP1: model s IV kotace = IV fixture, tedy stejné jako přehrání #1756
    assert (pricing.at_stop, pricing.at_tp1) == pytest.approx(
        (replay["at_stop"], replay["at_tp1"]), abs=1e-4
    )
    assert pricing.rr == pytest.approx(
        (replay["at_tp1"] - ask) / (ask - replay["at_stop"]), abs=1e-3
    )
    assert pricing.rr == pytest.approx(
        GOLDEN["expected"]["main"]["variants"]["hlavní"]["rr"], abs=1e-4
    )
    # Ask je o 1 % nad mid modelu plánu, R:R z kotace je proto pod 4,61 z modelu
    assert pricing.rr is not None and round(pricing.rr, 2) == 4.41
    assert pricing.rr_strict is not None and pricing.rr_strict < pricing.rr
    assert round(pricing.drawdown_usd) == -66
    assert pricing.loss_at_stop_pct is None and pricing.drawdown_pct is None  # účet se nečte


def test_golden_shadows() -> None:
    """Stín B: B 15:35 SEČ za 694,95 vyřazený P1, pak A jako hlavní; stín knotem = A 16:35."""
    shadow_b = _golden(SHADOW_B)
    want = GOLDEN["expected"]["entry_b"]
    b = shadow_b.trigger_b
    assert b is not None and b.kind == "B"
    assert b.ts == dt.datetime.fromisoformat(want["trigger_b"]["ts"])
    assert (round(b.price, 2), round(b.stop, 2)) == (
        want["trigger_b"]["price"],
        want["trigger_b"]["stop"],
    )
    assert shadow_b.entry is not None and shadow_b.entry.kind == "A"
    assert shadow_b.entry.ts == dt.datetime.fromisoformat(want["entry_ts"])
    wick = _golden(SHADOW_WICK)
    assert wick.entry is not None and wick.entry.kind == "A"
    assert wick.entry.ts == dt.datetime.fromisoformat(GOLDEN["expected"]["wick_test"]["entry_ts"])
    assert wick.trigger_b is None


def test_golden_result_json_keeps_shadows_apart() -> None:
    """JSON `result`: hlavní plán a stíny zvlášť, stíny se do hlavního nepočítají (K10 B)."""
    shadows = {
        "entry_b": plan_to_json(_plan(bars=(), rules=SHADOW_B)),
        "wick_test": plan_to_json(_plan(bars=(), rules=SHADOW_WICK)),
    }
    result = evaluate_option_plan(
        plan_to_json(_plan(bars=())),
        shadows,
        reasons=[],
        bars=GOLDEN_BARS,
        quotes=GOLDEN_QUOTES,
        max_loss_usd=None,
    )
    assert result["rules_version"] == 1
    assert result["main"]["status"] == "spuštěno" and result["main"]["rules"] == "hlavní"
    assert set(result["shadows"]) == {"entry_b", "wick_test"}
    assert result["shadows"]["entry_b"]["trigger_b"]["kind"] == "B"
    assert [v["budget"] for v in result["main"]["variants"]] == ["nenastaven", "nenastaven"]
    json.dumps(result)  # JSON bez NaN a nekonečna
    stats = track_record([(dt.date(2026, 11, 2), result)], since=dt.date(2026, 11, 2))
    trades = stats["main"]["variants"]["hlavní"]["trades"]
    assert trades["n"] == 1 and trades["preliminary"] is True
    assert trades["outcomes"]["TP2"] == 1
    assert trades["pnl_hold"]["sum_usd"] == pytest.approx(178.9213, abs=1e-3)
    assert trades["by_budget"]["nenastaven"]["n"] == 1
    assert stats["shadows"]["entry_b"]["variants"]["hlavní"]["trades"]["n"] == 1


def test_golden_shortfall_budget_class() -> None:
    """Rozpočet pod čerpáním P4 = `nad rozpočtem`, obchod zůstává obchodem (Rozhodnuto 16. 11.)."""
    result = _golden(max_loss_usd=50.0)
    assert [(v.kind, v.budget) for v in result.variants] == [
        ("obchod", "nad rozpočtem"),
        ("obchod", "nad rozpočtem"),
    ]


# ── ručně složený plán: stavy automatu ──────────────────────────────

DAY = dt.date(2026, 11, 17)  # úterý
OPEN = dt.datetime(2026, 11, 17, 14, 30, tzinfo=UTC)
EXIT = dt.datetime(2026, 11, 17, 20, 45, tzinfo=UTC)
EXPIRY = dt.date(2026, 11, 18)
STRIKES = (590.0, 589.0)


def part(**overrides: Any) -> dict[str, Any]:
    """Short plán z 9:15 ET bez spouště: flip 600, EM 5, cíle 595 / 593."""
    base: dict[str, Any] = {
        "rules": "hlavní",
        "day": DAY.isoformat(),
        "created_at": dt.datetime(2026, 11, 17, 14, 15, tzinfo=UTC).isoformat(),
        "direction": "short",
        "em": 5.0,
        "scenario_entry": 599.0,
        "targets": [595.0, 593.0],
        "target_ts": [
            dt.datetime(2026, 11, 17, 16, 30, tzinfo=UTC).isoformat(),
            dt.datetime(2026, 11, 17, 18, 0, tzinfo=UTC).isoformat(),
        ],
        "levels": {"flip": 600.0, "prev_low": 590.0},
        "exit_ts": EXIT.isoformat(),
        "p1": None,
        "variants": [
            {"role": role, "right": "P", "strike": strike, "expiry": EXPIRY.isoformat()}
            for role, strike in zip(("hlavní", "záložní"), STRIKES, strict=True)
        ],
        "missing": [],
    }
    base.update(overrides)
    return base


def fives(*blocks: tuple[float, float, float], start: dt.datetime = OPEN) -> list[Bar]:
    """1m bary po 5min blocích (high, low, close): high/low v první minutě, close všude."""
    out: list[Bar] = []
    for index, (high, low, close) in enumerate(blocks):
        block = start + index * 5 * MINUTE
        out.append(Bar(block, high, low, close))
        out.extend(Bar(block + i * MINUTE, close + 0.01, close - 0.01, close) for i in range(1, 5))
    return out


def flat(close: float, until: dt.datetime, start: dt.datetime) -> list[Bar]:
    n = int((until - start) / MINUTE)
    return [Bar(start + i * MINUTE, close + 0.01, close - 0.01, close) for i in range(n)]


def run(
    bars: Sequence[Bar],
    *,
    quotes: Sequence[Quote] | None = None,
    plan: dict[str, Any] | None = None,
    rules: Any = MAIN_RULES,
    max_loss_usd: float | None = 150.0,
) -> PlanResult:
    plan = plan if plan is not None else part()
    expiry = dt.date.fromisoformat(plan["variants"][0]["expiry"]) if plan["variants"] else EXPIRY
    return evaluate_plan(
        plan,
        rules=rules,
        bars=bars,
        quotes=quotes if quotes is not None else synthetic_quotes(bars, STRIKES, expiry),
        max_loss_usd=max_loss_usd,
    )


#: Test flipu 600 (5min close nad ním), pak první 5min close zpět = vstup A v 14:40 UTC
TEST_AND_ENTRY = ((600.6, 599.8, 600.4), (600.5, 599.4, 599.5))
#: SL = extrém od testu 600,6 + 0,05 EM = 600,85
STOP = 600.85


def until_exit(path: list[Bar], close: float) -> list[Bar]:
    return path + flat(close, EXIT, path[-1].ts + MINUTE)


def test_no_entry_until_exit() -> None:
    """Bez testu úrovně do exitu = `bez vstupu` (track record nespuštěno, ne ztráta)."""
    result = run(flat(598.0, EXIT, OPEN))
    assert (result.status, result.reasons) == ("bez vstupu", ("bez vstupu do exitu",))
    assert result.entry is None and result.variants == ()
    assert result.bars == result.bars_expected == 375


def test_no_level_a_within_one_em() -> None:
    """Úroveň A do 1 EM není: o vstupu rozhodl plán, bary ani jejich mezery nerozhodují."""
    plan = part(levels={"flip": 610.0})
    result = run([], plan=plan)
    assert result.status == "bez vstupu"
    assert result.reasons[0].startswith("vstup A není")


def test_stop_loss_before_tp1() -> None:
    """5min close nad SL před TP1 = SL za close; obě nohy P&L končí v SL."""
    path = fives(*TEST_AND_ENTRY, (600.9, 599.6, 600.9))
    result = run(until_exit(path, 601.0))
    assert result.status == "spuštěno" and result.outcome == "SL"
    assert result.entry is not None and result.entry.stop == pytest.approx(STOP)
    assert result.entry.ts == dt.datetime(2026, 11, 17, 14, 40, tzinfo=UTC)
    sl = result.events[-1]
    assert (sl.kind, sl.ts, sl.price) == (
        "SL",
        dt.datetime(2026, 11, 17, 14, 45, tzinfo=UTC),
        600.9,
    )
    main = result.variants[0]
    assert main.exit_tp1 is not None and main.exit_hold is not None
    assert main.exit_tp1 == main.exit_hold and main.exit_tp1.event == "SL"
    assert main.exit_tp1.pnl_usd is not None and main.exit_tp1.pnl_usd < 0


def test_stop_at_entry_after_tp1() -> None:
    """Po TP1 jde SL na vstup: 5min close nad vstupem = `TP1→vstup`."""
    path = fives(*TEST_AND_ENTRY, (599.0, 594.9, 597.0), (599.7, 597.0, 599.6))
    result = run(until_exit(path, 599.6))
    assert result.outcome == "TP1→vstup"
    assert [event.kind for event in result.events] == ["vstup", "TP1", "stop na vstupu"]
    tp1 = result.events[1]
    assert (tp1.ts, tp1.price) == (dt.datetime(2026, 11, 17, 14, 41, tzinfo=UTC), 595.0)
    main = result.variants[0]
    assert main.exit_tp1 is not None and main.exit_hold is not None
    assert (main.exit_tp1.event, main.exit_hold.event) == ("TP1", "stop na vstupu")
    assert main.exit_tp1.pnl_usd is not None and main.exit_hold.pnl_usd is not None
    assert main.exit_tp1.pnl_usd > main.exit_hold.pnl_usd


def test_exit_in_time() -> None:
    """Bez SL a cíle do exitu = výstup v exitu (15:45 ET) za close poslední minuty."""
    path = fives(*TEST_AND_ENTRY)
    result = run(until_exit(path, 597.5))
    assert result.outcome == "exit"
    exit_event = result.events[-1]
    assert (exit_event.kind, exit_event.ts, exit_event.price) == ("exit", EXIT, 597.5)
    main = result.variants[0]
    assert main.exit_hold is not None and main.exit_hold.event == "exit"
    # Kotace exitu = řádek minuty 15:44 ET (vzorek v jejím závěru)
    quotes = synthetic_quotes(until_exit(path, 597.5), STRIKES, EXPIRY)
    last = next(q for q in quotes if q.ts_min == EXIT - MINUTE and q.strike == 590.0)
    assert main.exit_hold.bid == pytest.approx(last.bid)


def _long(**overrides: Any) -> dict[str, Any]:
    """Zrcadlový long: flip 600 pod vstupem scénáře 601, cíle 605 / 607, call 610 a 611."""
    variants = [
        {"role": role, "right": "C", "strike": strike, "expiry": EXPIRY.isoformat()}
        for role, strike in (("hlavní", 610.0), ("záložní", 611.0))
    ]
    return part(
        direction="long",
        scenario_entry=601.0,
        targets=[605.0, 607.0],
        variants=variants,
        levels={"flip": 600.0},
        **overrides,
    )


#: Long: test flipu 600 (5min close pod ním), pak první 5min close zpět nad = vstup A 14:40
LONG_TEST_AND_ENTRY = ((600.2, 599.4, 599.6), (600.6, 599.7, 600.5))


def test_long_call_targets_on_high_and_stop_below() -> None:
    """Long/call: cíl na dotyk high, SL pod vstupem (extrém od testu − 0,05 EM)."""
    path = until_exit(
        fives(*LONG_TEST_AND_ENTRY, (605.1, 600.4, 604.0), (607.1, 603.9, 606.5)), 606.5
    )
    quotes = synthetic_quotes(path, (610.0, 611.0), EXPIRY, right="C")
    result = run(path, plan=_long(), quotes=quotes)
    assert result.entry is not None and result.entry.stop == pytest.approx(599.15)
    assert result.outcome == "TP2"
    assert [(e.kind, e.ts) for e in result.events] == [
        ("vstup", dt.datetime(2026, 11, 17, 14, 40, tzinfo=UTC)),
        ("TP1", dt.datetime(2026, 11, 17, 14, 41, tzinfo=UTC)),
        ("TP2", dt.datetime(2026, 11, 17, 14, 46, tzinfo=UTC)),
    ]
    main = result.variants[0]
    assert (main.right, main.strike, main.kind) == ("C", 610.0, "obchod")
    assert main.pricing is not None and main.pricing.drawdown_usd < 0
    assert main.pricing.at_stop < main.pricing.paid < main.pricing.at_tp1
    assert main.exit_hold is not None and main.exit_hold.pnl_usd is not None
    assert main.exit_hold.pnl_usd > 0


def test_long_stop_loss_on_close_below() -> None:
    path = until_exit(fives(*LONG_TEST_AND_ENTRY, (600.6, 598.9, 599.0)), 599.0)
    quotes = synthetic_quotes(path, (610.0, 611.0), EXPIRY, right="C")
    result = run(path, plan=_long(), quotes=quotes)
    assert result.outcome == "SL"
    assert (result.events[-1].ts, result.events[-1].price) == (
        dt.datetime(2026, 11, 17, 14, 45, tzinfo=UTC),
        599.0,
    )
    main = result.variants[0]
    assert main.exit_tp1 is not None and main.exit_tp1.pnl_usd is not None
    assert main.exit_tp1.pnl_usd < 0


def test_tp1_then_exit() -> None:
    path = fives(*TEST_AND_ENTRY, (599.0, 594.9, 597.0))
    result = run(until_exit(path, 597.0))
    assert result.outcome == "TP1→exit"
    assert [event.kind for event in result.events] == ["vstup", "TP1", "exit"]


def test_tp1_without_second_target() -> None:
    path = fives(*TEST_AND_ENTRY, (599.0, 594.9, 597.0))
    result = run(
        until_exit(path, 597.0), plan=part(targets=[595.0], target_ts=part()["target_ts"][:1])
    )
    assert result.outcome == "TP1"
    main = result.variants[0]
    assert main.exit_tp1 == main.exit_hold


def test_target_already_past_entry() -> None:
    """Vstup za cílem 1: R:R z kotace nedává smysl → vyřazeno P3 bez výsledku."""
    path = fives(*TEST_AND_ENTRY)
    plan = part(targets=[599.8, 593.0])
    result = run(until_exit(path, 597.0), plan=plan)
    assert result.outcome == "cíl 1 za vstupem"
    assert [event.kind for event in result.events] == ["vstup"]
    assert all(v.kind == "vyřazeno P3" and v.exit_tp1 is None for v in result.variants)


def test_rejected_p3_keeps_hypothetical_result() -> None:
    """R:R z kotace < 2 (široký spread) = `vyřazeno P3`, hypotetický výsledek se uloží."""
    path = until_exit(fives(*TEST_AND_ENTRY, (599.0, 592.9, 593.5)), 593.5)
    wide = synthetic_quotes(path, STRIKES, EXPIRY, bid_factor=0.6, ask_factor=1.8)
    result = run(path, quotes=wide)
    assert result.outcome == "TP2"
    for variant in result.variants:
        assert variant.kind == "vyřazeno P3"
        assert variant.pricing is not None
        assert variant.pricing.rr is not None and variant.pricing.rr < 2
        assert variant.exit_hold is not None and variant.exit_hold.pnl_usd is not None
    narrow = run(path)
    assert narrow.variants[0].kind == "obchod"


def test_missing_entry_quote_is_gap() -> None:
    """Bez kotace v minutě spouště = `bez kotace` (mezera), žádný odhad z modelu."""
    path = until_exit(fives(*TEST_AND_ENTRY), 597.5)
    quotes = [
        q
        for q in synthetic_quotes(path, STRIKES, EXPIRY)
        if not (q.strike == 590.0 and q.ts_min == dt.datetime(2026, 11, 17, 14, 39, tzinfo=UTC))
    ]
    result = run(path, quotes=quotes)
    main, backup = result.variants
    assert main.kind == "bez kotace" and main.pricing is None and main.exit_hold is None
    assert main.gaps and "kotace při vstupu" in main.gaps[0]
    assert backup.kind == "obchod"


def test_missing_exit_quote_leaves_pnl_gap() -> None:
    """Kotace v minutě výstupu chybí: P&L té nohy None s mezerou, ne poslední známá cena."""
    path = until_exit(fives(*TEST_AND_ENTRY, (599.0, 594.9, 597.0)), 597.0)
    quotes = [
        dataclasses.replace(q, bid=None, ask=None)
        if q.ts_min == dt.datetime(2026, 11, 17, 14, 40, tzinfo=UTC)
        else q
        for q in synthetic_quotes(path, STRIKES, EXPIRY)
    ]
    main = run(path, quotes=quotes).variants[0]
    assert main.kind == "obchod"
    assert main.exit_tp1 is not None and main.exit_tp1.bid is None and main.exit_tp1.pnl_usd is None
    assert main.exit_hold is not None and main.exit_hold.pnl_usd is not None
    assert any("TP1" in gap for gap in main.gaps)


def test_missing_bar_before_decision_is_gap() -> None:
    """Chybí 1m bar mezi US openem a rozhodnutím = `mezera`: minuta mohla nést spoušť nebo SL."""
    path = until_exit(fives(*TEST_AND_ENTRY), 597.5)
    holey = [bar for bar in path if bar.ts != dt.datetime(2026, 11, 17, 16, 0, tzinfo=UTC)]
    result = run(holey)
    assert result.status == "mezera" and result.variants == () and result.events == ()
    assert "chybí 1 z 375 minut" in result.reasons[0]
    assert (result.bars, result.bars_expected) == (374, 375)


def test_missing_bar_after_decision_does_not_matter() -> None:
    path = until_exit(fives(*TEST_AND_ENTRY, (600.9, 599.6, 600.9)), 601.0)
    holey = [bar for bar in path if bar.ts != dt.datetime(2026, 11, 17, 17, 0, tzinfo=UTC)]
    result = run(holey)
    assert result.status == "spuštěno" and result.outcome == "SL"


def test_no_bars_after_crash_is_gap() -> None:
    """Engine přestal zapisovat v 11:00 ET: bez spouště do té doby = mezera, ne `bez vstupu`."""
    result = run(flat(598.0, dt.datetime(2026, 11, 17, 16, 0, tzinfo=UTC), OPEN))
    assert result.status == "mezera"


def test_half_day_exit_at_1245_et() -> None:
    """Zkrácená seance 27. 11.: exit = settle 13:00 ET − 15 min = 12:45 ET (17:45 UTC)."""
    day = dt.date(2026, 11, 27)
    exit_ts = settle_ts(day) - dt.timedelta(minutes=15)
    assert exit_ts == dt.datetime(2026, 11, 27, 17, 45, tzinfo=UTC)
    start = dt.datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    plan = part(
        day=day.isoformat(),
        created_at=dt.datetime(2026, 11, 27, 14, 15, tzinfo=UTC).isoformat(),
        exit_ts=exit_ts.isoformat(),
        target_ts=[
            dt.datetime(2026, 11, 27, 16, 0, tzinfo=UTC).isoformat(),
            dt.datetime(2026, 11, 27, 17, 0, tzinfo=UTC).isoformat(),
        ],
        variants=[
            {"role": "hlavní", "right": "P", "strike": 590.0, "expiry": "2026-11-30"},
        ],
    )
    path = fives(*TEST_AND_ENTRY, start=start)
    bars = path + flat(597.5, settle_ts(day), path[-1].ts + MINUTE)  # bary až do close
    result = run(bars, plan=plan, quotes=synthetic_quotes(bars, (590.0,), dt.date(2026, 11, 30)))
    assert result.outcome == "exit"
    assert result.events[-1].ts == exit_ts
    assert result.bars_expected == int((exit_ts - start) / MINUTE)


def test_weekend_and_holiday_not_evaluated() -> None:
    """Plán vzniká jen v obchodní seanci; sobota nebo Díkůvzdání = chyba volajícího (#1309)."""
    for day in (dt.date(2026, 11, 21), dt.date(2026, 11, 26)):
        with pytest.raises(ValueError, match="bez obchodní seance"):
            run([], plan=part(day=day.isoformat()))


def test_plan_without_option() -> None:
    result = run([], plan=part(variants=[], missing=["řetěz QQQ 1DTE nedostupný"]))
    assert (result.status, result.reasons) == ("bez opce", ("řetěz QQQ 1DTE nedostupný",))


def test_plan_without_em_is_gap() -> None:
    result = run([], plan=part(em=None, missing=["EM QQQ: řetěz 0DTE bez zaplaceného straddlu"]))
    assert result.status == "mezera"


def test_rules_must_match_part() -> None:
    with pytest.raises(ValueError, match="pravidla"):
        run([], rules=SHADOW_B)


def test_late_sample_quote_is_accepted() -> None:
    """Vzorek F1b smí přijít do 10 s po závěru minuty (záporné stáří); uloží se k události."""
    path = until_exit(fives(*TEST_AND_ENTRY), 597.5)
    quotes = [
        dataclasses.replace(q, quote_age_s=-3.0) for q in synthetic_quotes(path, STRIKES, EXPIRY)
    ]
    main = run(path, quotes=quotes).variants[0]
    assert main.kind == "obchod"
    assert main.entry_quote is not None and main.entry_quote.quote_age_s == -3.0


# ── track record ────────────────────────────────────────────────────


def _result(path: list[Bar], **kwargs: Any) -> dict[str, Any]:
    return evaluate_option_plan(
        part(),
        {"entry_b": part(rules="stín vstup B"), "wick_test": part(rules="stín test knotem")},
        reasons=[],
        bars=path,
        quotes=synthetic_quotes(path, STRIKES, EXPIRY, **kwargs),
        max_loss_usd=150.0,
    )


def test_track_record_without_start_counts_nothing() -> None:
    """Bez data začátku (TRACK_RECORD_SINCE = None) je track record prázdný (Rozhodnuto 16. 11.)."""
    assert TRACK_RECORD_SINCE is None
    result = _result(until_exit(fives(*TEST_AND_ENTRY, (600.9, 599.6, 600.9)), 601.0))
    stats = track_record([(DAY, result)], since=TRACK_RECORD_SINCE)
    assert stats["since"] is None and stats["outside"] == 1
    assert stats["main"]["plans"] == 0 and stats["main"]["preliminary"] is True
    assert stats["main"]["variants"]["hlavní"]["trades"]["n"] == 0


def test_track_record_counts_from_start_and_splits_groups() -> None:
    """Od data začátku; obchody, vyřazené P3 a bez kotace zvlášť; stíny zvlášť."""
    sl = _result(until_exit(fives(*TEST_AND_ENTRY, (600.9, 599.6, 600.9)), 601.0))
    tp2 = _result(until_exit(fives(*TEST_AND_ENTRY, (599.0, 592.9, 593.5)), 593.5))
    rejected = _result(
        until_exit(fives(*TEST_AND_ENTRY, (599.0, 592.9, 593.5)), 593.5),
        bid_factor=0.6,
        ask_factor=1.8,
    )
    none = _result(flat(598.0, EXIT, OPEN))
    rows = [
        (dt.date(2026, 11, 13), tp2),  # před začátkem
        (DAY, sl),
        (DAY, tp2),
        (DAY, rejected),
        (DAY, none),
    ]
    stats = track_record(rows, since=DAY)
    assert (stats["since"], stats["outside"]) == ("2026-11-17", 1)
    main = stats["main"]
    assert main["plans"] == 4
    assert main["by_status"] == {"spuštěno": 3, "bez vstupu": 1, "bez opce": 0, "mezera": 0}
    trades = main["variants"]["hlavní"]["trades"]
    assert trades["n"] == 2
    assert (trades["outcomes"]["SL"], trades["outcomes"]["TP2"]) == (1, 1)
    assert trades["pnl_hold"]["n"] == 2 and trades["pnl_hold"]["wins"] == 1
    assert trades["by_budget"]["v rozpočtu"]["n"] + trades["by_budget"]["nad rozpočtem"]["n"] == 2
    rejected_p3 = main["variants"]["hlavní"]["rejected_p3"]
    assert rejected_p3["n"] == 1 and rejected_p3["outcomes"]["TP2"] == 1
    assert rejected_p3["pnl_hold"]["n"] == 1
    assert main["variants"]["hlavní"]["no_quote"] == 0
    assert stats["shadows"]["entry_b"]["plans"] == 4
    assert stats["shadows"]["wick_test"]["variants"]["záložní"]["trades"]["n"] == 2
