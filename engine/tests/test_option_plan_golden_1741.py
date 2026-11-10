"""Plán opce QQQ (#1741, ADR-0062): golden 2. 11. 2026 a pravidla čisté funkce.

Golden vstupy jsou z produkčních partic (`scripts/build_option_plan_golden_1741.py`). Řetěz
QQQ 1DTE je rekonstrukce (`readme_1dte` v datasetu). Očekávaná čísla spočítaly funkce
zpětného přehrání #1756, ne testovaný modul. AC #1741 platí pro IV 31,4 % (ATM IV NQ 1DTE
v 9:31 ET, jako přehrání); čísla s IV fixture (30 %) hlídá druhý případ.
"""

import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from gexlens_engine.compute.dayverdict import AutoScenario, LevelInputs
from gexlens_engine.compute.gexfield import _YEAR_S, bs_price
from gexlens_engine.compute.option_plan import (
    SHADOW_B,
    SHADOW_WICK,
    AccountParams,
    OptionPlan,
    PlanRules,
    build_option_plan,
    five_minute_bars,
    jsonable,
    plan_to_json,
)
from gexlens_engine.compute.scenario import Bar
from gexlens_engine.compute.settle import settle_ts

FIXTURE = Path(__file__).parent / "golden" / "option_plan_1741.json"
DATA: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
PRAGUE = ZoneInfo("Europe/Prague")
NOW = dt.datetime.fromisoformat(DATA["plan_ts"])
EXPIRY = dt.date.fromisoformat(DATA["expiry_1dte"])
RATIO = DATA["ratio_pair"]["nq"] / DATA["ratio_pair"]["qqq"]
SPOT = DATA["spot_nq"] / RATIO
#: Syntetické parametry účtu testu (ne skutečný účet; výchozí hodnota v kódu není)
ACCOUNT = AccountParams(account_usd=12_345.0, max_loss_usd=150.0)
#: Zrcadlo kolem 700 QQQ: long z téhož dne (q' = 1400 − q)
MIRROR = 1400.0


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


def _rows(raw: list[dict[str, Any]]) -> list[Row]:
    return [Row(**{**row, "ts_min": dt.datetime.fromisoformat(row["ts_min"])}) for row in raw]


CHAIN_0DTE = _rows(DATA["chain_0dte"])
CHAIN_1DTE = _rows(DATA["chain_1dte"])
BARS = [
    Bar(dt.datetime.fromisoformat(ts), high / RATIO, low / RATIO, close / RATIO)
    for ts, high, low, close in DATA["bars_nq"]
]
SCENARIO = AutoScenario(
    direction=DATA["scenario"]["direction"],
    entry=DATA["scenario"]["entry"],
    targets=tuple(DATA["scenario"]["targets"]),
    target_labels=tuple(DATA["scenario"]["target_labels"]),
    path=tuple((dt.datetime.fromisoformat(ts), price) for ts, price in DATA["scenario"]["path"]),
)
LEVELS = LevelInputs(**DATA["levels_nq"])


def _plan(**overrides: Any) -> OptionPlan:
    kwargs: dict[str, Any] = {
        "scenario": SCENARIO,
        "levels": LEVELS,
        "ratio": RATIO,
        "spot": SPOT,
        "chain_0dte": CHAIN_0DTE,
        "chain_1dte": CHAIN_1DTE,
        "expiry_1dte": EXPIRY,
        "account": ACCOUNT,
        "now": NOW,
        "bars": BARS,
    }
    kwargs.update(overrides)
    return build_option_plan(**kwargs)


def _with_iv(rows: list[Row], iv: float) -> list[Row]:
    return [dataclasses.replace(row, iv=iv) for row in rows]


def _hm(ts: dt.datetime) -> str:
    return ts.astimezone(PRAGUE).strftime("%H:%M")


# ── golden 2. 11. ───────────────────────────────────────────────────


def test_golden_inputs_k9a() -> None:
    """Vstupy plánu v 9:31 ET podle K9 A: poměr, spot, EM, převedené úrovně, P1."""
    plan = _plan(bars=())
    assert round(RATIO, 3) == 41.311
    assert round(SPOT, 2) == 695.26
    assert plan.em is not None and round(plan.em, 2) == 6.01
    assert plan.em_strike == 695
    assert plan.direction == "short" and plan.right == "P"
    assert [round(t, 2) for t in plan.targets] == [691.99, 690.59]
    assert plan.level_a is not None
    assert (plan.level_a[0], round(plan.level_a[1], 2)) == ("flip", 698.21)
    assert plan.p1 is not None and plan.p1.blocks
    assert plan.p1.strike == 700
    assert plan.p1.net_gex_1pct is not None and round(plan.p1.net_gex_1pct / 1e6) == 208
    assert plan.p1.distance_em is not None and round(plan.p1.distance_em, 2) == 0.79
    assert _hm(plan.exit_ts) == "21:45"
    assert plan.missing == ()


def test_golden_ac_at_nq_iv() -> None:
    """AC #1741, put 687 s IV 31,4 %: vstup A 16:35 za 697,99, SL 699,66.

    R:R 4,45 (přísně 2,69), P4 −70 $.
    """
    iv = DATA["iv_nq_1dte_atm"]
    assert round(iv, 3) == 0.314
    plan = _plan(chain_1dte=_with_iv(CHAIN_1DTE, iv))
    entry = plan.entry
    assert entry is not None and entry.kind == "A" and entry.level == "flip"
    assert _hm(entry.ts) == "16:35"
    assert (round(entry.price, 2), round(entry.stop, 2)) == (697.99, 699.66)
    main = plan.variants[0]
    assert (main.role, main.right, main.strike) == ("hlavní", "P", 687)
    assert main.pricing is not None
    assert main.pricing.rr is not None and round(main.pricing.rr, 2) == 4.45
    assert main.pricing.rr_strict is not None and round(main.pricing.rr_strict, 2) == 2.69
    assert round(main.pricing.drawdown_usd) == -70
    expected = DATA["expected"]["ac_iv_nq"]["hlavní"]
    _assert_pricing(plan, expected)


@pytest.mark.parametrize("case", ["fixture_iv", "ac_iv_nq"])
def test_golden_matches_replay(case: str) -> None:
    """Spoušť a model ceny shodné se zpětným přehráním #1756 (nezávislá implementace)."""
    expected = DATA["expected"][case]
    plan = _plan(chain_1dte=_with_iv(CHAIN_1DTE, expected["iv"]))
    _assert_pricing(plan, expected["hlavní"])


def _assert_pricing(plan: OptionPlan, expected: dict[str, Any]) -> None:
    entry = plan.entry
    assert entry is not None
    assert entry.kind == expected["kind"]
    assert entry.ts == dt.datetime.fromisoformat(expected["ts"])
    assert entry.price == pytest.approx(expected["price"], abs=1e-4)
    assert entry.stop == pytest.approx(expected["stop"], abs=1e-4)
    pricing = plan.variants[0].pricing
    assert pricing is not None
    for key in ("paid", "at_stop", "at_tp1", "rr", "rr_strict", "drawdown_usd"):
        assert getattr(pricing, key) == pytest.approx(expected[key], abs=1e-4), key


def test_golden_variants_on_fixture() -> None:
    """Hlavní put 687 (|delta| ≈ 0,25), záložní put 686; ceny se zdrojem a časem."""
    plan = _plan()
    main, backup = plan.variants
    assert (main.strike, backup.strike) == (687, 686)
    assert abs(abs(main.delta) - 0.25) < abs(abs(backup.delta) - 0.25)
    assert main.quote_ts == NOW and main.source == "tastytrade"
    assert main.mid == pytest.approx((main.bid + main.ask) / 2)
    assert len(main.at_targets) == 2 and main.at_targets[0] > main.mid
    assert plan.model_label == "model z naměřené IV"
    # Při IV fixture 30 %: R:R 4,61 (přísně 2,82), P4 −65 $ — obě varianty projdou P3 a P4
    assert main.pricing is not None and main.pricing.rr is not None
    assert round(main.pricing.rr, 2) == 4.61
    assert [v.verdict for v in plan.variants] == ["obchodovat", "obchodovat"]
    assert plan.verdict == "obchodovat"
    assert "699,66" in plan.p5 and "put 687" in plan.p5


def test_shadow_entry_b_blocked_by_p1() -> None:
    """Stín vstupu B (K10 B): B 15:35 za 694,95 vyřadí P1, plán vstoupí A jako hlavní."""
    plan = _plan(rules=SHADOW_B)
    b = plan.trigger_b
    assert b is not None and b.kind == "B"
    assert _hm(b.ts) == "15:35"
    assert (round(b.price, 2), round(b.stop, 2)) == (694.95, 698.51)
    for case in ("fixture_iv", "ac_iv_nq"):
        replay = DATA["expected"][case]["stín vstup B"]["trigger_b"]
        assert b.ts == dt.datetime.fromisoformat(replay["ts"])
        assert (b.price, b.stop) == pytest.approx((replay["price"], replay["stop"]), abs=1e-4)
    assert plan.p1 is not None and plan.p1.blocks
    assert plan.entry is not None and plan.entry.kind == "A" and _hm(plan.entry.ts) == "16:35"
    assert plan.rules == "stín vstup B"


def test_shadow_b_without_p1_block_enters_b() -> None:
    """Bez kladné GEX proti směru (P1 nepropustí jen blokující GEX) vstoupí stín v B."""
    no_gamma = [dataclasses.replace(row, gamma=None) for row in CHAIN_0DTE]
    plan = _plan(rules=SHADOW_B, chain_0dte=no_gamma)
    assert plan.p1 is None and "P1 bez dat: řetěz 0DTE bez gamma a OI" in plan.missing
    assert plan.entry is not None and plan.entry.kind == "B" and _hm(plan.entry.ts) == "15:35"
    main = _plan(chain_0dte=no_gamma)
    assert main.trigger_b is None and main.entry is not None and main.entry.kind == "A"
    assert main.missing == ()  # hlavní plán P1 nepotřebuje (K6 B)


def test_shadow_wick_same_entry() -> None:
    """Stín test knotem (K10 B) 2. 11. vstoupí stejně jako hlavní plán: A 16:35 za 697,99."""
    plan = _plan(rules=SHADOW_WICK)
    assert plan.entry is not None and plan.entry.kind == "A"
    assert _hm(plan.entry.ts) == "16:35" and round(plan.entry.price, 2) == 697.99
    assert plan.trigger_b is None


# ── vznik, rozpočet, mezery ─────────────────────────────────────────


def test_plan_at_creation_waits_for_entry_a() -> None:
    """V 9:31 ET bez barů: varianty čekají na vstup A, R:R se počítá až při spoušti."""
    plan = _plan(bars=())
    assert plan.entry is None
    assert [v.verdict for v in plan.variants] == ["čeká", "čeká"]
    assert plan.variants[0].pricing is None
    assert plan.variants[0].label == "čeká — čeká na vstup A (flip 698,21)"
    assert plan.verdict.startswith("čeká")
    assert plan.p5.startswith("SL se určí při spoušti vstupu A")


def test_bars_before_creation_do_not_trigger() -> None:
    """Spoušť jen z 5min barů uzavřených po vzniku plánu (bez pohledu dopředu)."""
    later = NOW.replace(hour=16)  # 17:31 SEČ, po spoušti 16:35; flip se pak už netestuje
    plan = _plan(now=later)
    assert plan.entry is None and plan.trigger_a is None
    assert plan.variants[0].reasons == ("bez vstupu do exitu",)


@pytest.mark.parametrize(
    "account",
    [
        AccountParams(account_usd=None, max_loss_usd=None),
        AccountParams(account_usd=12_345.0, max_loss_usd=None),
        AccountParams(account_usd=None, max_loss_usd=150.0),
        AccountParams(account_usd=0.0, max_loss_usd=150.0),
    ],
)
def test_account_not_set(account: AccountParams) -> None:
    """K2: nevyplněné parametry účtu = plán vznikne a obě varianty `neobchodovat`.

    Důvod: `rozpočet nenastaven`.
    """
    plan = _plan(account=account)
    assert len(plan.variants) == 2
    for variant in plan.variants:
        assert variant.verdict == "neobchodovat"
        assert variant.reasons[0].startswith("rozpočet nenastaven (chybí ")
    assert plan.verdict.startswith("neobchodovat — rozpočet nenastaven")
    assert ("velikost účtu" in plan.verdict) is not _positive(account.account_usd)
    assert ("rozpočet na obchod" in plan.verdict) is not _positive(account.max_loss_usd)


def _positive(value: float | None) -> bool:
    return value is not None and value > 0


def test_account_has_no_default() -> None:
    """Q3 B: žádná výchozí velikost účtu ani rozpočtu v kódu."""
    fields = dataclasses.fields(AccountParams)
    assert all(field.default is dataclasses.MISSING for field in fields)


def test_over_budget_marked_both_over_is_no_trade() -> None:
    """P4: varianta nad rozpočtem je označená, obě nad rozpočtem = neobchodovat."""
    full = _plan()
    main, backup = (v.pricing for v in full.variants)
    assert main is not None and backup is not None
    assert -backup.drawdown_usd < -main.drawdown_usd  # záložní je levnější
    between = -(main.drawdown_usd + backup.drawdown_usd) / 2
    plan = _plan(account=AccountParams(account_usd=12_345.0, max_loss_usd=between))
    assert plan.variants[0].verdict == "neobchodovat"
    assert any(r.startswith("nad rozpočtem (P4") for r in plan.variants[0].reasons)
    assert plan.variants[1].verdict == "obchodovat"
    assert plan.verdict == "obchodovat"
    tiny = _plan(account=AccountParams(account_usd=12_345.0, max_loss_usd=1.0))
    assert [v.verdict for v in tiny.variants] == ["neobchodovat", "neobchodovat"]
    assert tiny.verdict.startswith("neobchodovat — nad rozpočtem")


def test_percent_of_account() -> None:
    pricing = _plan().variants[0].pricing
    assert pricing is not None and pricing.drawdown_pct is not None
    assert pricing.drawdown_pct == pytest.approx(100 * pricing.drawdown_usd / 12_345.0)


def test_rr_below_two_is_no_trade() -> None:
    """P3: R:R opce pod 2 = neobchodovat (K8 A); stav zůstane spuštěný s cenou."""
    targets = (28_740.0, 28_529.0)  # cíl 1 těsně pod vstupem scénáře
    near = dataclasses.replace(
        SCENARIO,
        targets=targets,
        path=(
            SCENARIO.path[0],
            *((ts, t) for (ts, _), t in zip(SCENARIO.path[1:], targets, strict=True)),
        ),
    )
    plan = _plan(scenario=near)
    assert plan.entry is not None
    main = plan.variants[0]
    assert main.verdict == "neobchodovat"
    assert any("(P3)" in reason for reason in main.reasons)


def test_missing_level_is_visible() -> None:
    """Chybějící úroveň = viditelný důvod; vstup A pak vezme další úroveň do 1 EM (ONH)."""
    plan = _plan(levels=dataclasses.replace(LEVELS, flip=None))
    assert "chybí úroveň flip" in plan.missing
    assert plan.level_a is not None and plan.level_a[0] == "on_high"


def test_no_level_within_one_em() -> None:
    """Žádná úroveň proti směru do 1 EM = vstup A není, neobchodovat s důvodem (K3 bod 1)."""
    far = dataclasses.replace(
        LEVELS, flip=None, on_high=None, call_wall=None, prev_high=None, put_wall=None
    )
    plan = _plan(levels=far)
    assert plan.level_a is None and plan.entry is None
    reasons = " ".join(plan.variants[0].reasons)
    assert "vstup A není: žádná úroveň do 1 EM" in reasons
    assert "žádná úroveň proti směru" in reasons


def test_missing_chain_0dte() -> None:
    """Bez řetězu 0DTE není EM ani P1: plán bez vstupu s viditelným důvodem (K4 a)."""
    plan = _plan(chain_0dte=[])
    assert plan.em is None and plan.p1 is None and plan.entry is None
    assert "řetěz QQQ 0DTE nedostupný (EM, P1)" in plan.missing
    assert all("bez EM nejde určit vstup ani SL" in v.reasons for v in plan.variants)


def test_missing_chain_1dte() -> None:
    """Bez řetězu 1DTE plán vznikne bez opce s důvodem; úrovně a spoušť zůstanou (K4 a)."""
    plan = _plan(chain_1dte=[])
    assert plan.variants == ()
    assert plan.entry is not None and plan.level_a is not None
    assert plan.verdict == "neobchodovat — řetěz QQQ 1DTE nedostupný"


def test_missing_backup_strike() -> None:
    puts = [row for row in CHAIN_1DTE if row.right == "P" and row.strike >= 687]
    plan = _plan(chain_1dte=puts)
    assert [v.strike for v in plan.variants] == [687]
    assert "záložní varianta: v řetězu 1DTE není strike dál od peněz" in plan.missing


def test_missing_ratio_or_spot() -> None:
    plan = _plan(ratio=None)
    assert plan.missing == ("poměr NQ/QQQ chybí",) and plan.variants == ()
    assert plan.verdict == "neobchodovat — poměr NQ/QQQ chybí"
    assert _plan(spot=float("nan")).missing == ("spot QQQ chybí",)


def test_no_trigger_until_exit() -> None:
    """Bary do exitu bez testu úrovně = neobchodovat — bez vstupu do exitu."""
    start = NOW.replace(minute=30)
    flat = [
        Bar(start + dt.timedelta(minutes=i), 696.1, 695.9, 696.0)
        for i in range(int((settle_ts(NOW.date()) - start).total_seconds() // 60))
    ]
    plan = _plan(bars=flat)
    assert plan.entry is None
    assert plan.variants[0].reasons == ("bez vstupu do exitu",)


def test_single_target_priced_at_exit_k11a() -> None:
    """K11 A: scénář s jedním cílem (bod cesty v settle) oceňuje P3 nejpozději v exitu."""
    one = AutoScenario(
        direction="short",
        entry=SCENARIO.entry,
        targets=(SCENARIO.targets[0],),
        target_labels=(SCENARIO.target_labels[0],),
        path=(SCENARIO.path[0], (settle_ts(NOW.date()), SCENARIO.targets[0])),
    )
    plan = _plan(scenario=one)
    assert plan.entry is not None
    variant = plan.variants[0]
    pricing = variant.pricing
    assert pricing is not None and pricing.at_tp2 is None and pricing.pnl_tp2_usd is None
    tau = (settle_ts(EXPIRY) - plan.exit_ts).total_seconds() / _YEAR_S
    assert pricing.at_stop == pytest.approx(
        bs_price(plan.entry.stop, variant.strike, variant.iv, tau, "P")
    )
    assert pricing.rr == pytest.approx(pricing.rr_strict)  # SL i přísně v exitu


def test_long_mirrors_short() -> None:
    """Long zrcadlově: týž den zrcadlený kolem 700 QQQ dá vstup A ve stejný čas, call 713/714."""
    pivot = MIRROR / 2 * RATIO

    def m(value: float | None) -> float | None:
        return None if value is None else 2 * pivot - value

    levels = LevelInputs(
        flip=m(LEVELS.flip),
        call_wall=m(LEVELS.put_wall),
        put_wall=m(LEVELS.call_wall),
        centroid=m(LEVELS.centroid),
        prev_high=m(LEVELS.prev_low),
        prev_low=m(LEVELS.prev_high),
        prev_close=m(LEVELS.prev_close),
        on_high=m(LEVELS.on_low),
        on_low=m(LEVELS.on_high),
    )
    scenario = AutoScenario(
        direction="long",
        entry=2 * pivot - SCENARIO.entry,
        targets=tuple(2 * pivot - t for t in SCENARIO.targets),
        target_labels=SCENARIO.target_labels,
        path=tuple((ts, 2 * pivot - price) for ts, price in SCENARIO.path),
    )
    flip_side = {"C": "P", "P": "C"}

    def mirror_rows(rows: list[Row]) -> list[Row]:
        return [
            dataclasses.replace(
                row,
                strike=MIRROR - row.strike,
                right=flip_side[row.right],
                delta=None if row.delta is None else -row.delta,
            )
            for row in rows
        ]

    bars = [Bar(b.ts, MIRROR - b.low, MIRROR - b.high, MIRROR - b.close) for b in BARS]
    plan = _plan(
        scenario=scenario,
        levels=levels,
        spot=MIRROR - SPOT,
        chain_0dte=mirror_rows(CHAIN_0DTE),
        chain_1dte=mirror_rows(CHAIN_1DTE),
        bars=bars,
    )
    short = _plan()
    assert plan.right == "C" and plan.em == pytest.approx(short.em)
    assert plan.level_a is not None and plan.level_a[0] == "flip"
    assert plan.entry is not None and short.entry is not None
    assert plan.entry.ts == short.entry.ts
    assert plan.entry.price == pytest.approx(MIRROR - short.entry.price)
    assert plan.entry.stop == pytest.approx(MIRROR - short.entry.stop)
    assert [v.strike for v in plan.variants] == [MIRROR - 687, MIRROR - 686]
    pricing = plan.variants[0].pricing
    assert pricing is not None and pricing.at_tp1 > pricing.paid > pricing.at_stop
    assert "≤" in plan.p5 and "call 713" in plan.p5


def _at(now: dt.datetime, rows: list[Row]) -> list[Row]:
    return [dataclasses.replace(row, ts_min=now) for row in rows]


@pytest.mark.parametrize(
    "now",
    [
        dt.datetime(2026, 10, 31, 14, 31, tzinfo=dt.UTC),  # sobota
        dt.datetime(2026, 11, 1, 15, 0, tzinfo=dt.UTC),  # neděle před otevřením Globexu
        dt.datetime(2026, 11, 26, 15, 0, tzinfo=dt.UTC),  # Thanksgiving (NYSE zavřená)
    ],
)
def test_no_plan_outside_trading_session(now: dt.datetime) -> None:
    """Plán vzniká jen v obchodní seanci (`is_trading_session`, #1309)."""
    with pytest.raises(ValueError, match="obchodní seanci"):
        _plan(now=now, chain_0dte=_at(now, CHAIN_0DTE), chain_1dte=_at(now, CHAIN_1DTE))


@pytest.mark.parametrize(
    ("now", "expiry", "ok"),
    [
        (NOW, dt.date(2026, 11, 3), True),  # pondělí → úterý (golden)
        (dt.datetime(2026, 10, 30, 13, 31, tzinfo=dt.UTC), dt.date(2026, 11, 2), True),  # pá → po
        (dt.datetime(2026, 10, 30, 13, 31, tzinfo=dt.UTC), dt.date(2026, 10, 31), False),  # sobota
        (dt.datetime(2026, 11, 25, 14, 31, tzinfo=dt.UTC), dt.date(2026, 11, 26), False),  # svátek
        (NOW, NOW.date(), False),  # 0DTE není 1DTE
    ],
)
def test_expiry_1dte_is_next_trading_session(now: dt.datetime, expiry: dt.date, ok: bool) -> None:
    """1DTE = obchodní seance po dni plánu (ADR-0062 Odvozeno): pátek → pondělí, svátek ne."""
    kwargs: dict[str, Any] = {
        "now": now,
        "expiry_1dte": expiry,
        "chain_0dte": _at(now, CHAIN_0DTE),
        "chain_1dte": _at(now, CHAIN_1DTE),
    }
    if ok:
        plan = _plan(**kwargs)
        assert plan.day == now.date() and plan.variants[0].expiry == expiry
    else:
        with pytest.raises(ValueError, match="1DTE"):
            _plan(**kwargs)


def test_quotes_after_creation_raise() -> None:
    """Kotace řetězu po vzniku plánu = pohled dopředu, chyba volajícího."""
    early = NOW - dt.timedelta(minutes=10)
    with pytest.raises(ValueError, match="po vzniku plánu"):
        _plan(now=early)
    assert _plan().chain_0dte_ts == dt.datetime(2026, 11, 2, 14, 25, tzinfo=dt.UTC)


def test_rules_parameters_are_the_same_function() -> None:
    """Stíny K10 B jsou parametry téže funkce; hlavní pravidla vstup B nemají (K6 B)."""
    assert _plan().trigger_b is None
    assert _plan(rules=PlanRules("vlastní", entry_b=True)).trigger_b is not None


# ── 5min bary ───────────────────────────────────────────────────────


def test_five_minute_bars_only_closed() -> None:
    """Rozpracovaný 5min bar se nepoužije; bar s dírou je uzavřený, když přijde pozdější minuta."""
    start = dt.datetime(2026, 11, 2, 14, 30, tzinfo=dt.UTC)

    def bar(minute: int, close: float) -> Bar:
        return Bar(start + dt.timedelta(minutes=minute), close + 1, close - 1, close)

    partial = five_minute_bars(
        [bar(0, 1), bar(1, 2), bar(2, 3)], start, start + dt.timedelta(hours=1)
    )
    assert partial == ()
    gap = five_minute_bars([bar(0, 1), bar(2, 3), bar(6, 7)], start, start + dt.timedelta(hours=1))
    assert len(gap) == 1 and gap[0].end == start + dt.timedelta(minutes=5)
    assert (gap[0].high, gap[0].low, gap[0].close) == (4, 0, 3)
    full = five_minute_bars([bar(i, i) for i in range(5)], start, start + dt.timedelta(hours=1))
    assert len(full) == 1 and full[0].close == 4


# ── JSON pro option_plans (F2b #1742) ───────────────────────────────


def test_plan_to_json_nese_cely_plan_a_verdikt() -> None:
    """K2 A: plán jde do JSON celý, časy v UTC, verdikt plánu i hlavní slovo do sloupce."""
    waiting = _plan(bars=())
    data = plan_to_json(waiting)
    assert json.loads(json.dumps(data)) == data
    assert data["verdict"] == waiting.verdict and waiting.verdict_kind == "čeká"
    assert data["created_at"] == "2026-11-02T14:31:00+00:00"
    assert data["level_a"][0] == "flip" and data["p1"]["blocks"] is True
    assert [v["role"] for v in data["variants"]] == ["hlavní", "záložní"]
    assert data["variants"][0]["expiry"] == "2026-11-03"
    assert data["variants"][0]["pricing"] is None  # K12 A: R:R až při spoušti
    traded = _plan()
    assert traded.verdict_kind == "obchodovat"
    assert plan_to_json(traded)["entry"]["kind"] == "A"
    unset = _plan(bars=(), account=AccountParams(None, None))
    assert unset.verdict_kind == "neobchodovat"
    empty = _plan(bars=(), chain_1dte=[])
    assert empty.variants == () and empty.verdict_kind == "neobchodovat"


def test_json_odmitne_nan_a_naivni_cas() -> None:
    """Chyba výpočtu se neschová do `null`; cesta v chybě ukáže pole."""
    with pytest.raises(ValueError, match=r"plán\.ratio"):
        plan_to_json(dataclasses.replace(_plan(bars=()), ratio=float("nan")))
    with pytest.raises(ValueError, match="bez časové zóny"):
        jsonable({"ts": dt.datetime(2026, 11, 2, 14, 15)})
    with pytest.raises(TypeError, match="set"):
        jsonable({"x": {1, 2}})
