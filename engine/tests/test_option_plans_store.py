"""Tabulka `option_plans` (ADR-0062 K2 A, F2b #1742): jeden plán na scénář, nic se nepřepíše."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from gexlens_engine.storage.option_plans_store import OptionPlansRepository, effective_plan
from gexlens_engine.storage.scenarios_store import ScenariosRepository

DAY = dt.date(2026, 11, 2)
LABEL = dt.datetime(2026, 11, 2, 14, 15, tzinfo=dt.UTC)


def _repos(tmp_path: Path) -> tuple[ScenariosRepository, OptionPlansRepository]:
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}")
    scenarios = ScenariosRepository(db)
    scenarios.ensure_schema()
    plans = OptionPlansRepository(db)
    plans.ensure_schema()
    plans.ensure_schema()  # opakovaný start enginu
    return scenarios, plans


def _scenario(
    repo: ScenariosRepository, *, symbol: str = "NQ", day: dt.date = DAY, source: str = "auto"
) -> int:
    return repo.create(
        symbol=symbol,
        day=day,
        created_at=LABEL,
        deadline=day,
        deadline_ts=LABEL + dt.timedelta(hours=7),
        entry=28753.75,
        targets=[28587.0],
        path=[{"ts": LABEL.isoformat(), "price": 28753.75}],
        annotation_id=None,
        note=None,
        source=source,
    )


def test_plan_vznikne_jednou_a_neprepise_se(tmp_path: Path) -> None:
    scenarios, plans = _repos(tmp_path)
    scenario_id = _scenario(scenarios)
    plan_id = plans.create(
        scenario_id=scenario_id,
        day=DAY,
        created_at=LABEL,
        rules_version=1,
        verdict="čeká",
        status="sestaven",
        plan={"slot": "9:15", "main": {"strike": 687.0}},
    )
    assert plan_id is not None
    again = plans.create(
        scenario_id=scenario_id,
        day=DAY,
        created_at=LABEL + dt.timedelta(minutes=5),
        rules_version=1,
        verdict=None,
        status="bez opce",
        plan={"reasons": ["restart"]},
    )
    assert again is None
    row = plans.get_for_scenario(scenario_id)
    assert row is not None
    assert (row.id, row.status, row.verdict) == (plan_id, "sestaven", "čeká")
    assert row.plan == {"slot": "9:15", "main": {"strike": 687.0}}
    assert row.created_at == LABEL and row.created_at.tzinfo is not None
    assert (row.evaluated_at, row.result, row.image_path) == (None, None, None)
    assert plans.get_for_scenario(scenario_id + 1) is None


def test_scenar_dne_bez_planu_jen_auto_tehoz_dne_a_symbolu(tmp_path: Path) -> None:
    scenarios, plans = _repos(tmp_path)
    assert plans.orphan_auto_scenario("NQ", DAY) is None
    _scenario(scenarios, source="manual")
    _scenario(scenarios, symbol="ES")
    _scenario(scenarios, day=DAY - dt.timedelta(days=3))
    assert plans.orphan_auto_scenario("NQ", DAY) is None
    scenario_id = _scenario(scenarios)
    assert plans.orphan_auto_scenario("NQ", DAY) == (scenario_id, LABEL)
    plans.create(
        scenario_id=scenario_id,
        day=DAY,
        created_at=LABEL,
        rules_version=1,
        verdict=None,
        status="bez opce",
        plan={"reasons": ["restart"]},
    )
    assert plans.orphan_auto_scenario("NQ", DAY) is None


def test_preceneni_se_zapise_jednou_a_9_15_zustane(tmp_path: Path) -> None:
    """F2c #1769: klíč `plan.repricing` jednou za plán; JSON z 9:15 zůstane (K2 A).

    Sloupce se mění jen se `status` (plán sestavený celý v 9:31, vlastník 11. 11., #1738).
    """
    scenarios, plans = _repos(tmp_path)
    first = _scenario(scenarios)
    other_day = _scenario(scenarios, day=DAY + dt.timedelta(days=1))
    morning = {"slot": "9:15", "main": {"strike": 687.0}}
    plan_id = plans.create(
        scenario_id=first,
        day=DAY,
        created_at=LABEL,
        rules_version=1,
        verdict="čeká",
        status="sestaven",
        plan=morning,
    )
    other_id = plans.create(
        scenario_id=other_day,
        day=DAY + dt.timedelta(days=1),
        created_at=LABEL + dt.timedelta(days=1),
        rules_version=1,
        verdict=None,
        status="bez opce",
        plan={"reasons": ["restart"]},
    )
    assert plan_id is not None
    assert [row.id for row in plans.plans_of_day(DAY)] == [plan_id]
    assert plans.plans_of_day(DAY - dt.timedelta(days=1)) == []
    repricing = {"slot": "9:31", "reasons": [], "prices": {"main": [{"strike": 687.0}]}}
    assert plans.record_repricing(plan_id, repricing) is True
    assert plans.record_repricing(plan_id, {"slot": "9:31", "reasons": ["druhé"]}) is False
    (row,) = plans.plans_of_day(DAY)
    assert row.plan == {**morning, "repricing": repricing}
    assert (row.status, row.verdict, row.created_at) == ("sestaven", "čeká", LABEL)
    assert other_id is not None
    rebuilt = {"slot": "9:31", "reasons": [], "plan": {"main": {"strike": 686.0}}}
    assert plans.record_repricing(other_id, rebuilt, status="sestaven", verdict="čeká") is True
    assert plans.record_repricing(other_id, rebuilt, status="bez opce") is False
    (other,) = plans.plans_of_day(DAY + dt.timedelta(days=1))
    assert other.plan == {"reasons": ["restart"], "repricing": rebuilt}
    assert (other.status, other.verdict) == ("sestaven", "čeká")
    with pytest.raises(LookupError):
        plans.record_repricing(plan_id + 99, repricing)


def test_seznam_od_nejnovejsi_seance_a_cteni_podle_id(tmp_path: Path) -> None:
    """F4a #1743: seznam podle seance a `id` sestupně s limitem, neexistující id = None."""
    scenarios, plans = _repos(tmp_path)
    assert plans.list_plans(10) == []
    ids = []
    for offset in (1, 0, 1):
        day = DAY + dt.timedelta(days=offset)
        plan_id = plans.create(
            scenario_id=_scenario(scenarios, day=day),
            day=day,
            created_at=LABEL + dt.timedelta(days=offset),
            rules_version=1,
            verdict=None,
            status="bez opce",
            plan={"slot": "9:15", "reasons": ["restart"]},
        )
        assert plan_id is not None
        ids.append(plan_id)
    assert [row.id for row in plans.list_plans(10)] == [ids[2], ids[0], ids[1]]
    assert [row.id for row in plans.list_plans(1)] == [ids[2]]
    row = plans.get(ids[1])
    assert row is not None and (row.day, row.status) == (DAY, "bez opce")
    assert plans.get(max(ids) + 1) is None


MAIN_915 = {"rules": "hlavní", "variants": [{"strike": 687.0, "bid": 1.30, "ask": 1.36}]}
SHADOWS_915 = {"entry_b": {"rules": "stín vstup B"}, "wick_test": {"rules": "stín test knotem"}}
BUILT_915 = {
    "slot": "9:15",
    "reasons": [],
    "levels_nq": {"flip": 28_800.0},
    "chain": {"complete": True},
    "main": MAIN_915,
    "shadows": SHADOWS_915,
}
NO_OPTION_915 = {
    "slot": "9:15",
    "reasons": ["ranní snímek 9:15 ET nedorazil"],
    "levels_nq": {"flip": 28_800.0},
    "chain": None,
    "main": None,
    "shadows": {},
}


def _repricing(**values: object) -> dict[str, object]:
    return {"slot": "9:31", "reasons": [], "chain": {}, "prices": None, "plan": None, **values}


def test_platny_plan_z_9_15_pred_precenenim_a_s_cenami_z_9_31() -> None:
    """ADR-0062 K4 bod 10: úplný plán z 9:15, ceny z 9:31 vedle, mezery přecenění zvlášť."""
    before = effective_plan(BUILT_915)
    assert (before["slot"], before["main"], before["shadows"]) == ("9:15", MAIN_915, SHADOWS_915)
    assert (before["prices"], before["reasons"], before["repricing_reasons"]) == (None, [], None)
    prices = {"main": [{"strike": 687.0, "bid": 1.22, "ask": 1.26}], "entry_b": [], "wick_test": []}
    repriced = effective_plan({**BUILT_915, "repricing": _repricing(prices=prices)})
    assert (repriced["slot"], repriced["main"], repriced["prices"]) == ("9:15", MAIN_915, prices)
    assert repriced["repricing_reasons"] == []
    gap = ["snímek 9:31 ET neúplný: 1DTE bez kotace"]
    failed = effective_plan({**BUILT_915, "repricing": _repricing(reasons=gap)})
    assert (failed["main"], failed["prices"], failed["repricing_reasons"]) == (MAIN_915, None, gap)


def test_platny_plan_sestaveny_v_9_31_a_plan_bez_opce() -> None:
    """Plán bez opce z 9:15 sestavený v 9:31 platí celý z 9:31; jinak zůstane bez opce."""
    main_931 = {"rules": "hlavní", "variants": [{"strike": 686.0, "bid": 1.10, "ask": 1.14}]}
    rebuilt = {
        "slot": "9:31",
        "levels_nq": {"flip": 28_790.0},
        "main": main_931,
        "shadows": {"entry_b": {}, "wick_test": {}},
    }
    plan = effective_plan({**NO_OPTION_915, "repricing": _repricing(plan=rebuilt)})
    assert (plan["slot"], plan["main"], plan["levels_nq"]) == ("9:31", main_931, {"flip": 28_790.0})
    assert plan["shadows"] == {"entry_b": {}, "wick_test": {}} and plan["prices"] is None
    assert (plan["reasons"], plan["repricing_reasons"]) == (NO_OPTION_915["reasons"], [])
    gap = ["úrovně NQ minuty 9:31 ET nedorazily do 14:34:00 UTC"]
    none = effective_plan({**NO_OPTION_915, "repricing": _repricing(reasons=gap)})
    assert (none["slot"], none["main"], none["shadows"]) == ("9:15", None, {})
    assert (none["reasons"], none["repricing_reasons"]) == (NO_OPTION_915["reasons"], gap)
