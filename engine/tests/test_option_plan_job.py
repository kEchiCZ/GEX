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

import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.dayverdict import AutoScenario, LevelInputs
from gexlens_engine.compute.option_plan import AccountParams
from gexlens_engine.option_plan_job import (
    ACCOUNT_KEY,
    ALERT_KIND,
    MAX_LOSS_KEY,
    STATUS_BUILT,
    STATUS_NO_OPTION,
    OptionPlanMaker,
    account_params,
    snapshot_deadline,
)
from gexlens_engine.scenario_auto import CreatedScenario
from gexlens_engine.storage.option_plans_store import OptionPlanRow, OptionPlansRepository
from gexlens_engine.storage.parquet_store import SnapshotRow
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
        repository=plans, account=lambda: account, publisher=publisher, clock=_Clock(now)
    )
    return maker, plans, scenarios, publisher


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
        maker.on_snapshot(_snapshot(slot=REPRICE_SLOT))  # přecenění plán nečte
        await asyncio.sleep(0)
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
