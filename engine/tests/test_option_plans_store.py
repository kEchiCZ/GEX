"""Tabulka `option_plans` (ADR-0062 K2 A, F2b #1742): jeden plán na scénář, nic se nepřepíše."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from gexlens_engine.storage.option_plans_store import OptionPlansRepository
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
    """F2c #1769: klíč `plan.repricing` jednou za plán; 9:15 a sloupce beze změny (K2 A)."""
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
    plans.create(
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
    with pytest.raises(LookupError):
        plans.record_repricing(plan_id + 99, repricing)
