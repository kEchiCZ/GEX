"""Vznik plánu opce QQQ se scénářem dne v 9:15 ET (F2b #1742, ADR-0062 K4 C, body 5 a 8).

Řetěz, scénář a úrovně jsou z golden dne 2. 11. (`golden/option_plan_1741.json`). Řádky
řetězu nesou štítek hranice 9:15 ET, jak je zapíše ranní snímek (`tasty/plan_chain.py`).
"""

import asyncio
import dataclasses
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.dayverdict import AutoScenario, LevelInputs
from gexlens_engine.compute.option_plan import (
    ACCOUNT_KEY,
    MAX_LOSS_KEY,
    AccountParams,
    build_option_plan,
    plan_to_json,
)
from gexlens_engine.compute.scenario import Bar
from gexlens_engine.option_plan_job import (
    ALERT_KIND,
    CHANGE,
    REPRICE_ALERT_KIND,
    STATUS_BUILT,
    STATUS_NO_OPTION,
    OptionPlanMaker,
    account_params,
    levels_deadline,
    snapshot_deadline,
)
from gexlens_engine.scenario_auto import CreatedScenario, VerdictContext
from gexlens_engine.storage.option_plans_store import OptionPlanRow, OptionPlansRepository
from gexlens_engine.storage.parquet_store import LevelsRow, SnapshotRow
from gexlens_engine.storage.scenarios_store import ScenariosRepository
from gexlens_engine.tasty.plan_chain import (
    PLAN_SLOT,
    REPRICE_SLOT,
    ExpirySnapshot,
    PlanChainSnapshot,
    snapshot_boundary,
)

DATA: dict[str, Any] = json.loads(
    (Path(__file__).parent / "golden" / "option_plan_1741.json").read_text(encoding="utf-8")
)
MONDAY = dt.date(2026, 11, 2)
RATIO = DATA["ratio_pair"]["nq"] / DATA["ratio_pair"]["qqq"]
SPOT = DATA["spot_nq"] / RATIO
#: Syntetické parametry účtu testu (ne skutečný účet; výchozí hodnota v kódu není)
ACCOUNT = AccountParams(account_usd=12_345.0, max_loss_usd=150.0)
INCOMPLETE = "1DTE 20261103: put |delta| 0,25 = 687: soused 686 bez kotace s IV a delta do 40 s"


def _rows(raw: list[dict[str, Any]], ts: dt.datetime) -> tuple[SnapshotRow, ...]:
    return tuple(
        SnapshotRow(
            ts_min=ts,
            strike=row["strike"],
            right=row["right"],
            bid=row["bid"],
            ask=row["ask"],
            last=None,
            volume=None,
            iv=row["iv"],
            delta=row["delta"],
            gamma=row["gamma"],
            theta=None,
            vega=None,
            oi=row["oi"],
            stale_age=0.0,
        )
        for row in raw
    )


def _snapshot(
    session: dt.date = MONDAY,
    expiry_1dte: dt.date = dt.date(2026, 11, 3),
    *,
    reasons: tuple[str, ...] = (),
    slot: Any = PLAN_SLOT,
) -> PlanChainSnapshot:
    boundary = snapshot_boundary(session, slot)
    zero = _rows(DATA["chain_0dte"], boundary)
    one = _rows(DATA["chain_1dte"], boundary)
    return PlanChainSnapshot(
        session=session,
        slot=slot,
        started_at=boundary - dt.timedelta(seconds=90),
        spot_ts=boundary,
        spot=SPOT,
        spot_nq=DATA["spot_nq"],
        nq_source="ibkr",
        ratio=RATIO,
        ts=boundary + dt.timedelta(seconds=1),
        zero_dte=ExpirySnapshot(
            expiry=f"{session:%Y%m%d}",
            contracts=len(zero) + 4,
            quoted=len(zero),
            with_oi=len(zero) + 4,
            rows=zero,
            oi_missing=0,
            missing=(("1 změna po odběru (< 2)", ("660C", "661C", "729P", "730P")),),
        ),
        one_dte=ExpirySnapshot(
            expiry=f"{expiry_1dte:%Y%m%d}",
            contracts=len(one),
            quoted=len(one),
            with_oi=len(one),
            rows=one,
            oi_missing=0,
        ),
        reasons=reasons,
    )


def _created(
    session: dt.date = MONDAY, *, scenario_id: int = 35, late: dt.timedelta = dt.timedelta(0)
) -> CreatedScenario:
    shift = dt.datetime.combine(session, dt.time()) - dt.datetime.combine(MONDAY, dt.time())
    raw = DATA["scenario"]
    return CreatedScenario(
        scenario_id=scenario_id,
        session=session,
        created_at=snapshot_boundary(session, PLAN_SLOT) + late,
        scenario=AutoScenario(
            direction=raw["direction"],
            entry=raw["entry"],
            targets=tuple(raw["targets"]),
            target_labels=tuple(raw["target_labels"]),
            path=tuple((dt.datetime.fromisoformat(ts) + shift, price) for ts, price in raw["path"]),
        ),
        levels=LevelInputs(**DATA["levels_nq"]),
    )


class _Publisher:
    def __init__(self) -> None:
        self.alerts: list[dict[str, Any]] = []

    async def publish(self, channel: str, payload: dict[str, Any]) -> None:
        self.alerts.append({"channel": channel, **payload})


@dataclasses.dataclass
class _Clock:
    now: dt.datetime

    def __call__(self) -> dt.datetime:
        return self.now


def _maker(
    tmp_path: Path, now: dt.datetime, account: AccountParams = ACCOUNT
) -> tuple[OptionPlanMaker, OptionPlansRepository, ScenariosRepository, _Publisher]:
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}")
    scenarios = ScenariosRepository(db)
    scenarios.ensure_schema()
    plans = OptionPlansRepository(db)
    plans.ensure_schema()
    publisher = _Publisher()
    maker = OptionPlanMaker(
        repository=plans,
        account=lambda: account,
        scenarios=scenarios,
        day_levels=_day_levels,
        publisher=publisher,
        clock=_Clock(now),
    )
    return maker, plans, scenarios, publisher


async def _day_levels(session: dt.date) -> VerdictContext:
    """ONH/ONL a PDH/PDL/PDC dne v 9:31 (`gather_day_levels`): golden 2. 11."""
    raw = DATA["levels_nq"]
    return VerdictContext(
        prev_high=raw["prev_high"],
        prev_low=raw["prev_low"],
        prev_close=raw["prev_close"],
        on_high=raw["on_high"],
        on_low=raw["on_low"],
    )


def _row(plans: OptionPlansRepository, scenario_id: int = 35) -> OptionPlanRow:
    row = plans.get_for_scenario(scenario_id)
    assert row is not None
    return row


BOUNDARY = snapshot_boundary(MONDAY, PLAN_SLOT)


def test_snimek_pred_scenarem_da_hlavni_plan_a_oba_stiny(tmp_path: Path) -> None:
    """Pořadí 10. 11.: snímek skončil v 9:15:01 ET, scénář se zapsal v 9:16:08 ET."""
    maker, plans, _, publisher = _maker(tmp_path, BOUNDARY + dt.timedelta(seconds=68))
    maker.on_snapshot(_snapshot())
    plan_id = asyncio.run(maker.make(_created()))
    row = _row(plans)
    assert (row.id, row.status, row.verdict) == (plan_id, STATUS_BUILT, "čeká")
    assert (row.day, row.created_at, row.rules_version) == (MONDAY, BOUNDARY, 1)
    main = row.plan["main"]
    assert main["created_at"] == "2026-11-02T14:15:00+00:00" and main["rules"] == "hlavní"
    assert main["verdict"].startswith("čeká — čeká na vstup A (flip")
    assert [v["pricing"] for v in main["variants"]] == [None, None]  # K12 A
    assert main["trigger_b"] is None  # K6 B: hlavní plán vstup B nemá
    assert set(row.plan["shadows"]) == {"entry_b", "wick_test"}
    assert row.plan["shadows"]["entry_b"]["rules"] == "stín vstup B"
    assert row.plan["shadows"]["entry_b"]["p1"]["strike"] == 700
    # P1 stínu B z řetězu 9:15 a k němu podíl kotací pásma 0DTE (Rozhodnuto 10. 11.: A)
    zero = row.plan["chain"]["zero_dte"]
    assert (zero["quoted"], zero["contracts"]) == (84, 88)
    assert zero["quoted_share"] == pytest.approx(84 / 88)
    assert zero["missing"] == {"1 změna po odběru (< 2)": ["660C", "661C", "729P", "730P"]}
    assert row.plan["chain"]["source"] == "tastytrade" and row.plan["chain"]["complete"]
    assert row.plan["levels_nq"]["flip"] == DATA["levels_nq"]["flip"]
    assert row.plan["reasons"] == []
    (alert,) = publisher.alerts
    assert (alert["channel"], alert["kind"], alert["symbol"]) == ("alerts", ALERT_KIND, "QQQ")
    assert (alert["plan_id"], alert["scenario_id"]) == (plan_id, 35)
    assert "PUT" in alert["message"] and "hlavní put" in alert["message"]
    assert "12345" not in alert["message"] and "12 345" not in alert["message"]


def test_scenar_pred_snimkem_pocka_a_cyklus_minuty_nece(tmp_path: Path) -> None:
    """Pořadí scénář → snímek: `on_scenario` hned vrátí úlohu, plán vznikne po snímku."""
    maker, plans, _, _ = _maker(tmp_path, BOUNDARY + dt.timedelta(seconds=20))

    async def run() -> int | None:
        task = maker.on_scenario(_created())
        await asyncio.sleep(0)
        assert not task.done()  # čeká na snímek, minutový cyklus už jede dál
        reprice = maker.on_snapshot(_snapshot(slot=REPRICE_SLOT))  # 9:31 vznik nespustí
        assert reprice is not None and await reprice == []  # plán ještě není
        assert not task.done()
        maker.on_snapshot(_snapshot())
        return await asyncio.wait_for(task, 5)

    plan_id = asyncio.run(run())
    assert plan_id is not None and _row(plans).status == STATUS_BUILT


def test_neuplny_snimek_da_plan_bez_opce_s_duvodem(tmp_path: Path) -> None:
    """K4 bod 5: neúplný ranní snímek = bez opce, žádná čísla z poloviny řetězu."""
    maker, plans, _, publisher = _maker(tmp_path, BOUNDARY + dt.timedelta(seconds=68))
    maker.on_snapshot(_snapshot(reasons=(INCOMPLETE,)))
    asyncio.run(maker.make(_created()))
    row = _row(plans)
    assert (row.status, row.verdict) == (STATUS_NO_OPTION, None)
    assert row.plan["main"] is None and row.plan["shadows"] == {}
    assert row.plan["reasons"] == [f"ranní snímek 9:15 ET neúplný: {INCOMPLETE}"]
    assert row.plan["chain"]["complete"] is False
    assert row.plan["chain"]["reasons"] == [INCOMPLETE]
    assert "bez opce" in publisher.alerts[0]["message"]


def test_snimek_nedorazil_do_limitu(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Start enginu po hranici nebo výpadek tasty: plán po limitu bez opce, ne zmrzlá čísla."""
    late = snapshot_deadline(MONDAY) + dt.timedelta(seconds=1)
    assert snapshot_deadline(MONDAY) == BOUNDARY + dt.timedelta(seconds=100)
    maker, plans, _, _ = _maker(tmp_path, late)
    maker.on_snapshot(_snapshot(session=dt.date(2026, 10, 30), expiry_1dte=MONDAY))  # pátek
    with caplog.at_level(logging.WARNING):
        asyncio.run(maker.make(_created()))
    row = _row(plans)
    assert row.status == STATUS_NO_OPTION and row.plan["chain"] is None
    assert row.plan["reasons"] == ["ranní snímek 9:15 ET nedorazil do 14:16:40 UTC"]
    assert "bez opce" in caplog.text  # trh otevřený: WARNING


def test_pozdni_scenar_da_plan_bez_opce(tmp_path: Path) -> None:
    """Scénář se štítkem 9:17 ET (1. 10.) by míchal úrovně 9:17 s řetězem 9:15."""
    maker, plans, _, _ = _maker(tmp_path, BOUNDARY + dt.timedelta(minutes=3))
    maker.on_snapshot(_snapshot())
    asyncio.run(maker.make(_created(late=dt.timedelta(minutes=2))))
    row = _row(plans)
    assert row.status == STATUS_NO_OPTION and row.created_at == BOUNDARY + dt.timedelta(minutes=2)
    assert row.plan["reasons"] == [
        "pozdní scénář: štítek 09:17 ET, ranní snímek je z 9:15 ET (K4 bod 5)"
    ]


def test_restart_plan_existuje_nic_se_neprepise(tmp_path: Path) -> None:
    maker, plans, scenarios, publisher = _maker(tmp_path, BOUNDARY + dt.timedelta(seconds=68))
    scenario_id = _scenario(scenarios)
    maker.on_snapshot(_snapshot())
    first = asyncio.run(maker.make(_created(scenario_id=scenario_id)))
    again = asyncio.run(maker.make(_created(scenario_id=scenario_id, late=dt.timedelta(minutes=1))))
    assert first is not None and again is None
    assert _row(plans, scenario_id).status == STATUS_BUILT
    assert len(publisher.alerts) == 1
    assert asyncio.run(maker.recover(BOUNDARY + dt.timedelta(minutes=5))) is None


def test_restart_po_vzniku_scenaru_da_plan_bez_opce_jen_do_exitu(tmp_path: Path) -> None:
    """Generátor po restartu skončí na `exists_auto`; scénář bez plánu najde `recover`."""
    maker, plans, scenarios, _ = _maker(tmp_path, BOUNDARY)
    scenario_id = _scenario(scenarios)
    # Po exitu (settle − 15 min = 20:45 UTC) a o víkendu nic: nasazení v pauze Globexu
    assert asyncio.run(maker.recover(dt.datetime(2026, 11, 2, 22, 30, tzinfo=dt.UTC))) is None
    assert asyncio.run(maker.recover(dt.datetime(2026, 11, 7, 15, 0, tzinfo=dt.UTC))) is None
    assert plans.get_for_scenario(scenario_id) is None
    restart = BOUNDARY + dt.timedelta(minutes=5)
    plan_id = asyncio.run(maker.recover(restart))
    row = _row(plans, scenario_id)
    assert (row.id, row.status, row.created_at) == (plan_id, STATUS_NO_OPTION, restart)
    assert row.plan["reasons"] == [
        "engine restartoval po vzniku scénáře (09:15 ET) — ranní snímek 9:15 ET v tomto běhu není"
    ]
    assert asyncio.run(maker.recover(restart + dt.timedelta(minutes=1))) is None


def test_nevyplnene_parametry_uctu_plan_vznikne_neobchodovat(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """K2 A: bez rozpočtu plán vznikne a varianty říkají proč; tichý default není."""
    with caplog.at_level(logging.WARNING):
        unset = account_params({ACCOUNT_KEY: "abc", MAX_LOSS_KEY: True})
    assert unset == AccountParams(None, None)
    assert "abc" not in caplog.text and MAX_LOSS_KEY in caplog.text
    assert account_params({}) == AccountParams(None, None)
    assert account_params({ACCOUNT_KEY: "1500", MAX_LOSS_KEY: 120}) == AccountParams(1500.0, 120.0)
    assert account_params({ACCOUNT_KEY: "nan"}).account_usd is None
    # Táž pravidla jako `PUT /settings` (#1743): prázdná hodnota = nenastaveno bez WARNING,
    # nula a záporné číslo = neplatné s WARNING bez hodnoty
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert account_params({ACCOUNT_KEY: "", MAX_LOSS_KEY: None}) == AccountParams(None, None)
    assert caplog.text == ""
    with caplog.at_level(logging.WARNING):
        assert account_params({ACCOUNT_KEY: 0, MAX_LOSS_KEY: -75}) == AccountParams(None, None)
    assert "-75" not in caplog.text and "neplatné" in caplog.text
    maker, plans, _, _ = _maker(tmp_path, BOUNDARY + dt.timedelta(seconds=68), account=unset)
    maker.on_snapshot(_snapshot())
    asyncio.run(maker.make(_created()))
    row = _row(plans)
    assert (row.status, row.verdict) == (STATUS_BUILT, "neobchodovat")
    assert "rozpočet nenastaven" in row.plan["main"]["verdict"]


def test_vikend_a_svatek_plan_nevznikne_patek_bere_pondelni_1dte(tmp_path: Path) -> None:
    """`is_trading_session`, ne `weekday()`: so, ne a Díkůvzdání bez plánu, pátek → 1DTE pondělí."""
    friday, monday = dt.date(2026, 11, 6), dt.date(2026, 11, 9)
    maker, plans, _, _ = _maker(
        tmp_path, snapshot_boundary(friday, PLAN_SLOT) + dt.timedelta(seconds=68)
    )
    closed = (dt.date(2026, 11, 7), dt.date(2026, 11, 8), dt.date(2026, 11, 26))
    for scenario_id, day in enumerate(closed, start=7):
        assert asyncio.run(maker.make(_created(day, scenario_id=scenario_id))) is None
        assert plans.get_for_scenario(scenario_id) is None
        noon = dt.datetime.combine(day, dt.time(15), tzinfo=dt.UTC)
        assert asyncio.run(maker.recover(noon)) is None
    maker.on_snapshot(_snapshot(session=friday, expiry_1dte=monday))
    asyncio.run(maker.make(_created(friday, scenario_id=36)))
    row = _row(plans, 36)
    assert row.status == STATUS_BUILT and row.day == friday
    assert {v["expiry"] for v in row.plan["main"]["variants"]} == {"2026-11-09"}


def _scenario(repo: ScenariosRepository) -> int:
    return repo.create(
        symbol="NQ",
        day=MONDAY,
        created_at=BOUNDARY,
        deadline=MONDAY,
        deadline_ts=BOUNDARY + dt.timedelta(hours=6, minutes=45),
        entry=DATA["scenario"]["entry"],
        targets=list(DATA["scenario"]["targets"]),
        path=[{"ts": ts, "price": price} for ts, price in DATA["scenario"]["path"]],
        annotation_id=None,
        note=None,
        source="auto",
        rationale={"verdict": "short", "targets": list(DATA["scenario"]["target_labels"])},
    )


def test_chyba_sestaveni_da_plan_bez_opce_s_duvodem(tmp_path: Path) -> None:
    """Pád čtení parametrů nebo výpočtu nezanechá mezeru jen v logu (AGENTS.md: fail fast)."""
    maker, plans, _, publisher = _maker(tmp_path, BOUNDARY + dt.timedelta(seconds=68))

    def broken() -> AccountParams:
        raise RuntimeError("settings nedostupné")

    maker.account = broken
    maker.on_snapshot(_snapshot())

    async def run() -> int | None:
        return await maker.on_scenario(_created())

    plan_id = asyncio.run(run())
    row = _row(plans)
    assert (row.id, row.status, row.verdict) == (plan_id, STATUS_NO_OPTION, None)
    assert row.plan["reasons"] == ["plán se nesestavil (RuntimeError: settings nedostupné)"]
    assert "bez opce" in publisher.alerts[0]["message"]


# ── přecenění v 9:31 ET (F2c #1769, ADR-0062 K4 C body 2, 4–6 a bod 10) ─────

AT_931 = snapshot_boundary(MONDAY, REPRICE_SLOT)
NQ_931 = LevelsRow(
    ts_min=AT_931,
    flip=DATA["levels_nq"]["flip"],
    call_wall=DATA["levels_nq"]["call_wall"],
    put_wall=DATA["levels_nq"]["put_wall"],
    centroid=DATA["levels_nq"]["centroid"],
    total_gex=1.0e9,
)
PRAGUE = ZoneInfo("Europe/Prague")


def _snapshot_931(
    change: Any = None, *, reasons: tuple[str, ...] = (), ts: dt.datetime = AT_931
) -> PlanChainSnapshot:
    """Snímek 9:31 z golden řetězu; `change(row)` upraví řádky 1DTE (None = vynechat)."""
    base = _snapshot(slot=REPRICE_SLOT, reasons=reasons)
    assert base.one_dte is not None and base.zero_dte is not None
    one = [dataclasses.replace(row, ts_min=ts) for row in base.one_dte.rows]
    if change is not None:
        one = [new for new in (change(row) for row in one) if new is not None]
    zero = tuple(dataclasses.replace(row, ts_min=ts) for row in base.zero_dte.rows)
    return dataclasses.replace(
        base,
        zero_dte=dataclasses.replace(base.zero_dte, rows=zero),
        one_dte=dataclasses.replace(base.one_dte, rows=tuple(one)),
    )


def _built_plan(tmp_path: Path) -> tuple[OptionPlanMaker, OptionPlansRepository, _Publisher, int]:
    """Úplný plán z 9:15 (sestaven), hodiny přenastavené na 9:31:05 ET."""
    maker, plans, scenarios, publisher = _maker(tmp_path, BOUNDARY + dt.timedelta(seconds=68))
    scenario_id = _scenario(scenarios)
    maker.on_snapshot(_snapshot())
    plan_id = asyncio.run(maker.make(_created(scenario_id=scenario_id)))
    assert plan_id is not None
    maker.clock = _Clock(AT_931 + dt.timedelta(seconds=5))
    return maker, plans, publisher, scenario_id


def _plan_without_option(
    tmp_path: Path, account: AccountParams = ACCOUNT
) -> tuple[OptionPlanMaker, OptionPlansRepository, _Publisher, int]:
    """2. 11.: ranní řetěz uložený není → plán v 9:15 bez opce (K4 bod 5).

    Všechny úrovně minuty 9:15 se liší od 9:31, ať test pozná, že plán z 9:31 je nebere.
    """
    maker, plans, scenarios, publisher = _maker(
        tmp_path, snapshot_deadline(MONDAY) + dt.timedelta(seconds=1), account
    )
    scenario_id = _scenario(scenarios)
    created = _created(scenario_id=scenario_id)
    shifted = {
        name: value + 40.0
        for name, value in dataclasses.asdict(created.levels).items()
        if value is not None
    }
    created = dataclasses.replace(created, levels=dataclasses.replace(created.levels, **shifted))
    assert asyncio.run(maker.make(created)) is not None
    maker.clock = _Clock(AT_931 + dt.timedelta(seconds=5))
    return maker, plans, publisher, scenario_id


def _reprice(maker: OptionPlanMaker, snapshot: PlanChainSnapshot, *levels: LevelsRow) -> list[int]:
    async def run() -> list[int]:
        for row in levels:
            maker.on_levels(row)
        written = await maker.reprice(snapshot)
        await asyncio.gather(*maker._tasks)  # hlídač vidí snímek a nic nepíše
        return written

    return asyncio.run(run())


def _watch(maker: OptionPlanMaker, *levels: LevelsRow) -> None:
    """Minuty pipeline NQ bez snímku 9:31: hlídač doběhne (limit je za hodinami testu)."""

    async def run() -> None:
        for row in levels:
            maker.on_levels(row)
        await asyncio.gather(*maker._tasks)

    asyncio.run(run())


def test_golden_2_11_plan_bez_opce_v_9_31_cely_z_9_31_se_zmenou(tmp_path: Path) -> None:
    """AC 1: plán z 9:31 dává čísla K9 A (16:35 SEČ za 697,99, SL 699,66) a zprávu „ZMĚNA“."""
    maker, plans, publisher, scenario_id = _plan_without_option(tmp_path)
    morning = _row(plans, scenario_id).plan
    assert _reprice(maker, _snapshot_931(), NQ_931) == [_row(plans, scenario_id).id]
    row = _row(plans, scenario_id)
    # Část z 9:15 zůstává (K2 A); sloupce popisují plán z 9:31 (vlastník 11. 11., #1738)
    assert {key: row.plan[key] for key in morning} == morning
    assert (row.status, row.verdict, row.created_at) == (STATUS_BUILT, "čeká", BOUNDARY)
    repricing = row.plan["repricing"]
    assert (repricing["slot"], repricing["reasons"], repricing["prices"]) == ("9:31", [], None)
    assert repricing["chain"]["slot"] == "9:31" and repricing["chain"]["complete"]
    rebuilt = repricing["plan"]
    # Všechny úrovně z 9:31 (minuta NQ a bary), žádná z 9:15 (K4 bod 5, golden K9 A)
    levels = rebuilt["levels_nq"]
    assert levels == {**DATA["levels_nq"], "em_anchor": None, "em_points": None}
    assert morning["levels_nq"]["on_high"] == DATA["levels_nq"]["on_high"] + 40.0
    main = rebuilt["main"]
    assert main["created_at"] == "2026-11-02T14:31:00+00:00"
    assert main["level_a"][0] == "flip" and round(main["level_a"][1], 2) == 698.21
    assert [round(target, 2) for target in main["targets"]] == [691.99, 690.59]
    assert round(main["em"], 2) == 6.01
    assert [(v["role"], v["strike"]) for v in main["variants"]] == [
        ("hlavní", 687.0),
        ("záložní", 686.0),
    ]
    assert main["verdict"] == "čeká — čeká na vstup A (flip 698,21)"
    assert set(rebuilt["shadows"]) == {"entry_b", "wick_test"}
    # Uložený plán = čistá funkce v 9:31 ze týchž vstupů; s bary po vzniku dá K9 A
    raw = DATA["scenario"]
    scenario = AutoScenario(
        direction=raw["direction"],
        entry=raw["entry"],
        targets=tuple(raw["targets"]),
        target_labels=tuple(raw["target_labels"]),
        path=tuple((dt.datetime.fromisoformat(ts), price) for ts, price in raw["path"]),
    )
    snap = _snapshot_931()
    assert snap.zero_dte is not None and snap.one_dte is not None
    inputs: dict[str, Any] = {
        "scenario": scenario,
        "levels": LevelInputs(**levels),
        "ratio": RATIO,
        "spot": SPOT,
        "chain_0dte": snap.zero_dte.rows,
        "chain_1dte": snap.one_dte.rows,
        "expiry_1dte": dt.date(2026, 11, 3),
        "account": ACCOUNT,
        "now": AT_931,
    }
    assert plan_to_json(build_option_plan(**inputs)) == main
    bars = [
        Bar(dt.datetime.fromisoformat(ts), high / RATIO, low / RATIO, close / RATIO)
        for ts, high, low, close in DATA["bars_nq"]
    ]
    entry = build_option_plan(**inputs, bars=bars).entry
    assert entry is not None and entry.kind == "A"
    assert entry.ts.astimezone(PRAGUE).strftime("%H:%M") == "16:35"
    assert (round(entry.price, 2), round(entry.stop, 2)) == (697.99, 699.66)
    created, repriced = publisher.alerts
    assert created["kind"] == ALERT_KIND and "bez opce" in created["message"]
    assert (repriced["kind"], repriced["plan_id"], repriced["ts"]) == (
        REPRICE_ALERT_KIND,
        row.id,
        AT_931.timestamp(),
    )
    message = repriced["message"]
    assert message.startswith(f"{CHANGE}: Plán QQQ #{row.id}") and "(PUT)" in message
    assert "neobchodovat — bez opce (ranní snímek 9:15 ET nedorazil" in message
    assert message.endswith("→ čeká — čeká na vstup A (flip 698,21)")
    assert "hlavní put 687 (3. 11.)" in message


def test_sestaveni_bez_rozpoctu_neni_zmena(tmp_path: Path) -> None:
    """K4 bod 4: „ZMĚNA“ jen při změně čeká × neobchodovat; bez opce → rozpočet nenastaven ne."""
    unset = AccountParams(None, None)
    maker, plans, publisher, scenario_id = _plan_without_option(tmp_path, unset)
    _reprice(maker, _snapshot_931(), NQ_931)
    row = _row(plans, scenario_id)
    assert (row.status, row.verdict) == (STATUS_BUILT, "neobchodovat")
    main = row.plan["repricing"]["plan"]["main"]
    assert main["verdict"].startswith("neobchodovat — rozpočet nenastaven")
    message = publisher.alerts[-1]["message"]
    assert message.startswith("Plán QQQ #") and CHANGE not in message
    assert "→ neobchodovat — rozpočet nenastaven" in message


def test_uplny_plan_preceni_kontrakty_z_9_15_stav_beze_zmeny(tmp_path: Path) -> None:
    """AC 2: strike, úrovně, vstup A a EM zůstanou, mění se jen kotace, IV a model."""
    maker, plans, publisher, scenario_id = _built_plan(tmp_path)
    before = _row(plans, scenario_id)

    def later(row: SnapshotRow) -> SnapshotRow:
        assert row.bid is not None and row.ask is not None
        return dataclasses.replace(row, bid=row.bid + 0.1, ask=row.ask + 0.12, iv=0.33)

    assert _reprice(maker, _snapshot_931(later), NQ_931) == [before.id]
    row = _row(plans, scenario_id)
    assert {key: row.plan[key] for key in before.plan} == before.plan
    assert (row.status, row.verdict) == (before.status, before.verdict) == (STATUS_BUILT, "čeká")
    repricing = row.plan["repricing"]
    assert repricing["reasons"] == [] and repricing["plan"] is None
    prices = repricing["prices"]
    assert set(prices) == {"main", "entry_b", "wick_test"}
    morning = {v["strike"]: v for v in before.plan["main"]["variants"]}
    for key in prices:
        assert [(p["role"], p["strike"]) for p in prices[key]] == [
            ("hlavní", 687.0),
            ("záložní", 686.0),
        ]
    for price in prices["main"]:
        was = morning[price["strike"]]
        assert price["bid"] == pytest.approx(was["bid"] + 0.1)
        assert price["ask"] == pytest.approx(was["ask"] + 0.12)
        assert price["iv"] == 0.33 and price["quote_ts"] == "2026-11-02T14:31:00+00:00"
        assert was["quote_ts"] == "2026-11-02T14:15:00+00:00"
        assert len(price["at_targets"]) == 2 and price["at_targets"] != was["at_targets"]
        assert price["expiry"] == "2026-11-03" and price["source"] == "tastytrade"
    (alert,) = [a for a in publisher.alerts if a["kind"] == REPRICE_ALERT_KIND]
    message = alert["message"]
    assert not message.startswith(CHANGE)
    assert "přeceněn v 9:31 ET: hlavní put 687" in message and "(v 9:15 " in message
    assert message.endswith("Stav beze změny: " + before.plan["main"]["verdict"])


def test_neuplny_snimek_9_31_necha_ceny_z_9_15_s_duvodem(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """K4 bod 6: přecenění se nezapíše, důvod jde do plánu, platí ceny z 9:15."""
    maker, plans, publisher, scenario_id = _built_plan(tmp_path)
    reason = "1DTE 20261103: OI 70/84 < 90 %"
    with caplog.at_level(logging.WARNING):
        _reprice(maker, _snapshot_931(reasons=(reason,)))
    repricing = _row(plans, scenario_id).plan["repricing"]
    assert repricing["reasons"] == [f"snímek 9:31 ET neúplný: {reason}"]
    assert (repricing["prices"], repricing["plan"]) == (None, None)
    assert repricing["chain"]["complete"] is False
    message = publisher.alerts[-1]["message"]
    assert "přecenění 9:31 ET se nezapsalo" in message and message.endswith("z 9:15 ET")
    assert "se nezapsalo" in caplog.text  # trh otevřený: WARNING


def test_kontrakt_planu_chybi_ve_snimku_9_31(tmp_path: Path) -> None:
    """Komentář 9. 11.: úplný snímek 9:31 bez kontraktu plánu = přecenění se nezapíše."""
    maker, plans, _, scenario_id = _built_plan(tmp_path)

    def without_686p(row: SnapshotRow) -> SnapshotRow | None:
        return None if (row.strike, row.right) == (686.0, "P") else row

    _reprice(maker, _snapshot_931(without_686p))
    repricing = _row(plans, scenario_id).plan["repricing"]
    assert repricing["reasons"] == ["záložní put 686 bez kotace s IV a delta"]
    assert repricing["prices"] is None


def test_kotace_po_9_31_je_chyba_a_duvod_v_planu(tmp_path: Path) -> None:
    """Bez pohledu dopředu: řádky 9:32 v přecenění 9:31 = ValueError, mezera v tabulce."""
    maker, plans, publisher, scenario_id = _built_plan(tmp_path)
    _reprice(maker, _snapshot_931(ts=AT_931 + dt.timedelta(minutes=1)))
    (reason,) = _row(plans, scenario_id).plan["repricing"]["reasons"]
    assert reason.startswith("přecenění se nesestavilo (ValueError: řetěz má")
    assert "po přecenění 14:31 UTC" in reason
    assert publisher.alerts[-1]["kind"] == REPRICE_ALERT_KIND


def test_oba_snimky_neuplne_plan_zustane_bez_opce(tmp_path: Path) -> None:
    maker, plans, publisher, scenario_id = _plan_without_option(tmp_path)
    reason = "0DTE 20261102: ATM straddle 695 bez zaplacené kotace put"
    _reprice(maker, _snapshot_931(reasons=(reason,)), NQ_931)
    row = _row(plans, scenario_id)
    assert (row.status, row.verdict, row.plan["main"]) == (STATUS_NO_OPTION, None, None)
    repricing = row.plan["repricing"]
    assert repricing["reasons"] == [f"snímek 9:31 ET neúplný: {reason}"]
    assert repricing["plan"] is None
    message = publisher.alerts[-1]["message"]
    assert "zůstává bez opce (9:31 ET)" in message and not message.startswith(CHANGE)


def test_bez_urovni_nq_9_31_plan_zustane_bez_opce(tmp_path: Path) -> None:
    """Přetažená minuta 9:31 cyklu NQ: úrovně 9:32 by byly pohled dopředu, plán čeká do 9:34."""
    maker, plans, _, scenario_id = _plan_without_option(tmp_path)
    assert levels_deadline(MONDAY) == AT_931 + dt.timedelta(minutes=3)
    maker.clock = _Clock(levels_deadline(MONDAY) + dt.timedelta(seconds=1))
    later = dataclasses.replace(NQ_931, ts_min=AT_931 + dt.timedelta(minutes=1))
    _reprice(maker, _snapshot_931(), later)
    assert _row(plans, scenario_id).plan["repricing"]["reasons"] == [
        "úrovně NQ minuty 9:31 ET nedorazily do 14:34:00 UTC"
    ]


def test_selhane_cteni_onh_a_pd_z_api_necha_plan_bez_opce(tmp_path: Path) -> None:
    """Výpadek API: úrovně z poloviny by daly jinou úroveň A, plán zůstane bez opce s důvodem."""
    maker, plans, _, scenario_id = _plan_without_option(tmp_path)

    async def broken(session: dt.date) -> VerdictContext:
        return VerdictContext(failures=["bars dnes: HTTP 503"])

    maker.day_levels = broken
    _reprice(maker, _snapshot_931(), NQ_931)
    assert _row(plans, scenario_id).plan["repricing"]["reasons"] == [
        "ONH/ONL a PDH/PDL/PDC z API: bars dnes: HTTP 503"
    ]


def test_plan_z_restartu_se_sestavi_smazany_scenar_ne(tmp_path: Path) -> None:
    """Plán z `recover` (bez úrovní z 9:15) se v 9:31 sestaví celý; smazaný scénář ne."""
    maker, plans, scenarios, _ = _maker(tmp_path, BOUNDARY + dt.timedelta(minutes=5))
    restarted = _scenario(scenarios)
    assert asyncio.run(maker.recover()) is not None
    assert _row(plans, restarted).plan["levels_nq"] is None
    deleted = _scenario(scenarios)
    asyncio.run(maker.make(_created(scenario_id=deleted, late=dt.timedelta(minutes=2))))
    scenarios.delete(deleted)
    maker.clock = _Clock(AT_931 + dt.timedelta(seconds=5))
    _reprice(maker, _snapshot_931(), NQ_931)
    rebuilt = _row(plans, restarted).plan["repricing"]
    assert rebuilt["reasons"] == [] and rebuilt["plan"]["main"]["created_at"].endswith(
        "14:31:00+00:00"
    )
    assert [v["strike"] for v in rebuilt["plan"]["main"]["variants"]] == [687.0, 686.0]
    assert _row(plans, restarted).status == STATUS_BUILT
    gone = _row(plans, deleted)
    assert gone.plan["repricing"]["reasons"] == [f"scénář dne #{deleted} v PG už není"]
    assert (gone.status, gone.verdict) == (STATUS_NO_OPTION, None)


def test_restart_mezi_9_31_a_close_jedno_preceneni(tmp_path: Path) -> None:
    """Druhá instance enginu nad toutéž DB: přecenění ani důvod hlídače se nepřidají."""
    maker, plans, publisher, scenario_id = _built_plan(tmp_path)
    assert len(_reprice(maker, _snapshot_931())) == 1
    first = _row(plans, scenario_id).plan["repricing"]
    later = AT_931 + dt.timedelta(minutes=30)
    again, _, _, publisher_2 = _maker(tmp_path, later)
    _watch(again, dataclasses.replace(NQ_931, ts_min=later))  # start po 9:31, plán přeceněný
    assert _reprice(again, _snapshot_931()) == []
    assert _row(plans, scenario_id).plan["repricing"] == first
    assert publisher_2.alerts == [] and len(publisher.alerts) == 2


def test_snimek_9_31_nedorazil_hlidac_zapise_duvod(tmp_path: Path) -> None:
    """Chyba snímku nebo tasty mimo provoz: plán dostane důvod po limitu, ne mlčení (B3)."""
    maker, plans, publisher, scenario_id = _built_plan(tmp_path)
    late = snapshot_deadline(MONDAY, REPRICE_SLOT) + dt.timedelta(seconds=1)
    maker.clock = _Clock(late)
    _watch(maker, NQ_931)
    repricing = _row(plans, scenario_id).plan["repricing"]
    assert repricing["reasons"] == ["snímek 9:31 ET nedorazil do 14:32:40 UTC"]
    assert (repricing["prices"], repricing["plan"], repricing["chain"]) == (None, None, None)
    assert publisher.alerts[-1]["ts"] == late.timestamp()
    assert "přecenění 9:31 ET se nezapsalo" in publisher.alerts[-1]["message"]


def test_start_po_9_31_hlidac_zapise_duvod_jen_do_exitu(tmp_path: Path) -> None:
    """Start enginu po 9:31: snímek se nedohání. Před 9:31, po exitu a o víkendu nic."""
    _, _, _, scenario_id = _built_plan(tmp_path)
    exit_ts = dt.datetime(2026, 11, 2, 20, 45, tzinfo=dt.UTC)
    saturday = dt.datetime(2026, 11, 7, 15, tzinfo=dt.UTC)
    for now in (AT_931 - dt.timedelta(minutes=1), exit_ts, saturday):
        quiet, plans, _, _ = _maker(tmp_path, now)
        _watch(quiet, dataclasses.replace(NQ_931, ts_min=now))
        assert quiet._watched == set() and "repricing" not in _row(plans, scenario_id).plan
    restart = AT_931 + dt.timedelta(minutes=9)
    late, plans, _, publisher = _maker(tmp_path, restart)
    _watch(late, dataclasses.replace(NQ_931, ts_min=restart))
    assert _row(plans, scenario_id).plan["repricing"]["reasons"] == [
        "engine startoval po 9:31 ET — snímek přecenění se v tomto běhu nedohání"
    ]
    (alert,) = publisher.alerts
    assert alert["ts"] == restart.timestamp()


def test_vikend_svatek_a_pondeli_proti_patku(tmp_path: Path) -> None:
    """`is_trading_session`: so, ne a Díkůvzdání bez přecenění; pondělí páteční plán nečte."""
    friday, monday = dt.date(2026, 11, 6), dt.date(2026, 11, 9)
    maker, plans, _, _ = _maker(
        tmp_path, snapshot_boundary(friday, PLAN_SLOT) + dt.timedelta(seconds=68)
    )
    maker.on_snapshot(_snapshot(session=friday, expiry_1dte=monday))
    assert asyncio.run(maker.make(_created(friday, scenario_id=36))) is not None
    for day in (dt.date(2026, 11, 7), dt.date(2026, 11, 8), dt.date(2026, 11, 26), monday):
        boundary = snapshot_boundary(day, REPRICE_SLOT)
        snap = dataclasses.replace(
            _snapshot(session=day, slot=REPRICE_SLOT), spot_ts=boundary, ts=boundary
        )
        assert _reprice(maker, snap, dataclasses.replace(NQ_931, ts_min=boundary)) == []
    assert "repricing" not in _row(plans, 36).plan
    # Víkend a svátek úrovně ani hlídače nemají, pondělí ano
    assert list(maker._levels) == [monday] and maker._watched == {monday}


def test_preceneni_minutovy_cyklus_neceka(tmp_path: Path) -> None:
    """`on_snapshot` (9:31) a `on_levels` hned vrátí; plán bez opce počká na úrovně NQ."""
    maker, plans, _, scenario_id = _plan_without_option(tmp_path)

    async def run() -> list[int]:
        task = maker.on_snapshot(_snapshot_931())
        assert task is not None
        for _ in range(5):
            await asyncio.sleep(0.01)
        assert not task.done()  # čeká na úrovně minuty 9:31
        maker.on_levels(NQ_931)
        written = await asyncio.wait_for(task, 5)
        await asyncio.wait_for(asyncio.gather(*maker._tasks), 5)  # hlídač vidí snímek
        return written

    assert asyncio.run(run()) == [_row(plans, scenario_id).id]
    assert _row(plans, scenario_id).plan["repricing"]["plan"]["main"] is not None
