"""Stádia buněk ticker × šablona (#1323, Knihovna setupů fáze 1).

Auto / Stín / Zkouška v parametrech setupů, priorita bloků v `_risk_context`,
čerpání zkoušky (včetně otevřeného setupu), konec zkoušky → Auto s alertem
jednou na zkoušku a regrese Auto = chování před #1323.
"""

import datetime as dt
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import create_engine

import gexlens_engine.setups as setups_module
from gexlens_engine.compute.risk import RealizedSetup, trial_usage
from gexlens_engine.compute.settle import session_bounds
from gexlens_engine.compute.setups import (
    SETUP_MECHANICS_VERSION,
    TRIAL_BUDGET_R_RANGE,
    TRIAL_BUDGET_SETUPS_RANGE,
    Direction,
    MinuteInputs,
    SetupCandidate,
    SetupParams,
    SetupTemplate,
    TrialCell,
    params_from_dict,
    params_to_dict,
    parse_cell,
    with_stage,
)
from gexlens_engine.compute.walkforward import WalkForwardResult
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.setups import SetupEngine
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.setup_params_store import SetupParamsRepository
from gexlens_engine.storage.setups_store import SetupsRepository

SESSION = dt.date(2026, 9, 16)  # středa
NOW = session_bounds(SESSION)[0] + dt.timedelta(hours=16)  # 16. 9. 14:00 UTC
CELL = "ES:failed_break"

#: Verze 1 parameter store produkce (seed 8. 9. 2026, SELECT 1. 10. 2026) —
#: bez risk klíčů i bez stádií; musí se načíst jako dnešní chování (vše Auto)
PROD_V1 = json.loads(
    '{"wall_zone": 3.0, "wall_zone_atr": 0.0, "rejection_min": 1.0, "rejection_min_atr": 0.0,'
    ' "break_min_atr": 0.0, "reclaim_min_atr": 0.0, "divergence_lookback": 10, "min_rrr": 1.2,'
    ' "atr_lookback": 14, "min_risk_atr": 2.0, "max_rr": 3.0, "break_min": 3.0,'
    ' "acceptance_minutes": 5, "reclaim_window": 15, "reclaim_min": 1.0,'
    ' "pin_max_minutes": 180.0, "pin_min_distance": 8.0, "pin_stop_ratio": 0.75,'
    ' "pin_stability": 5.0, "pin_stability_lookback": 60, "momentum_break": 2.0,'
    ' "momentum_flow_share": 0.6, "momentum_flow_lookback": 10, "momentum_cross_window": 10,'
    ' "momentum_cum_quantile": 0.25, "trend_ema_span": 20, "trend_pullback_atr": 0.5,'
    ' "trend_rejection_atr": 0.25, "trend_min_distance_atr": 12.0, "cooldown_minutes": 10,'
    ' "min_wall_dominance": 0.15, "spring_lookback": 90, "spring_rejection": 1.0,'
    ' "spring_stop_buffer": 2.0, "counter_flow_lookback": 30,'
    ' "counter_stop_cooldown_minutes": 45, "max_stops_per_direction": 3,'
    ' "confidence_min_samples": 30, "direction_block_minutes": 90,'
    ' "disabled_templates": ["divergence_spring"]}'
)


# ── Parametry: serializace a validace ───────────────────────────────


def test_stadia_projdou_dictem_i_store(tmp_path: Path) -> None:
    started = dt.datetime(2026, 10, 1, 14, 5, tzinfo=dt.UTC)
    params = SetupParams(
        shadow_cells=frozenset({"ES:trend_continuation"}),
        trial_cells=(TrialCell("NQ:trend_continuation", started, 10, 3.0, 5),),
    )
    as_dict = params_to_dict(params)
    assert as_dict["shadow_cells"] == ["ES:trend_continuation"]
    assert as_dict["trial_cells"] == {
        "NQ:trend_continuation": {
            "started_at": "2026-10-01T14:05:00+00:00",
            "budget_setups": 10,
            "budget_r": 3.0,
            "mechanics_version": 5,
        }
    }
    assert as_dict["trial_budget_setups"] == 10 and as_dict["trial_budget_r"] == 3.0
    assert params_from_dict(json.loads(json.dumps(as_dict))) == params
    repo = SetupParamsRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'p.sqlite'}"))
    repo.ensure_schema()
    repo.save(params, note="zkouška NQ T7", created_by="ui")
    latest = repo.latest()
    assert latest is not None and latest.params == params
    assert latest.params.stage_of("NQ", "trend_continuation") == "trial"
    assert latest.params.stage_of("ES", "trend_continuation") == "shadow"
    assert latest.params.stage_of("NQZ6", "trend_continuation") == "auto"  # ADR-0041


def test_verze_1_produkce_se_nacte_jako_vse_auto(tmp_path: Path) -> None:
    """Regrese: verze bez stádií (jediná na produkci) = dnešní chování."""
    loaded = params_from_dict(PROD_V1)
    assert loaded.shadow_cells == frozenset() and loaded.trial_cells == ()
    assert loaded == SetupParams()  # seed = defaulty kódu
    for template in SetupTemplate:
        assert loaded.stage_of("ES", template.value) == "auto"
        assert loaded.trial_of("NQ", template.value) is None
    # Týmž čtením jako engine: řádek store s JSON verze 1
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'p.sqlite'}")
    repo = SetupParamsRepository(db)
    repo.ensure_schema()
    from gexlens_engine.storage.setup_params_store import setup_params_table

    with db.begin() as conn:
        conn.execute(
            setup_params_table.insert().values(
                created_ts=dt.datetime(2026, 9, 8, 22, 49, tzinfo=dt.UTC),
                created_by="engine",
                note="seed",
                mechanics_version=5,
                params=PROD_V1,
            )
        )
    latest = repo.latest()
    assert latest is not None and latest.version == 1 and latest.params == SetupParams()


@pytest.mark.parametrize(
    ("values", "match"),
    [
        ({"shadow_cells": ["ES-failed_break"]}, "Špatný formát buňky"),
        ({"shadow_cells": ["es:failed_break"]}, "ticker piš jako 'ES'"),
        ({"shadow_cells": [":failed_break"]}, "Špatný formát buňky"),
        ({"shadow_cells": ["ES:"]}, "Špatný formát buňky"),
        ({"shadow_cells": ["ES:fib_618"]}, "Neznámá šablona"),
        ({"shadow_cells": ["E S:failed_break"]}, "Špatný formát buňky"),
        (
            {
                "shadow_cells": [CELL],
                "trial_cells": {
                    CELL: {
                        "started_at": "2026-10-01T14:05:00+00:00",
                        "budget_setups": 10,
                        "budget_r": 3.0,
                        "mechanics_version": 5,
                    }
                },
            },
            "zároveň ve stínu i ve zkoušce",
        ),
        (
            {
                "trial_cells": {
                    CELL: {
                        "started_at": "2026-10-01T14:05:00+00:00",
                        "budget_r": 3,
                        "mechanics_version": 5,
                    }
                }
            },
            "právě klíče",
        ),
        (
            # Zkouška bez mechaniky by po jejím zvednutí neskončila
            {
                "trial_cells": {
                    CELL: {
                        "started_at": "2026-10-01T14:05:00+00:00",
                        "budget_setups": 5,
                        "budget_r": 3,
                    }
                }
            },
            "právě klíče",
        ),
        (
            {
                "trial_cells": {
                    CELL: {
                        "started_at": "2026-10-01T14:05:00",
                        "budget_setups": 5,
                        "budget_r": 3,
                        "mechanics_version": 5,
                    }
                }
            },
            "časovou zónu",
        ),
        (
            {
                "trial_cells": {
                    CELL: {
                        "started_at": "2026-10-01T14:05:00Z",
                        "budget_setups": True,
                        "budget_r": 3,
                        "mechanics_version": 5,
                    }
                }
            },
            "celé číslo",
        ),
        (
            {
                "trial_cells": {
                    CELL: {
                        "started_at": "2026-10-01T14:05:00Z",
                        "budget_setups": 5,
                        "budget_r": 3,
                        "mechanics_version": "5",
                    }
                }
            },
            "mechanics_version musí být celé číslo",
        ),
        (
            {
                "trial_cells": {
                    CELL: {
                        "started_at": "2026-10-01T14:05:00Z",
                        "budget_setups": 5,
                        "budget_r": 3,
                        "mechanics_version": 0,
                    }
                }
            },
            "mechanics_version musí být ≥ 1",
        ),
        ({"trial_cells": [CELL]}, "objekt"),
        ({"trial_budget_setups": 0}, "mimo meze 1–20"),
        ({"trial_budget_setups": 21}, "mimo meze 1–20"),
        ({"trial_budget_r": 0.4}, "mimo meze 0.5–6 R"),
        ({"trial_budget_r": 6.5}, "mimo meze 0.5–6 R"),
    ],
)
def test_stadia_validace(values: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        params_from_dict(values)


def test_meze_rozpoctu_zkousky() -> None:
    assert TRIAL_BUDGET_SETUPS_RANGE == (1, 20) and TRIAL_BUDGET_R_RANGE == (0.5, 6.0)
    start = dt.datetime(2026, 10, 1, 14, 5, tzinfo=dt.UTC)
    for n, r in ((1, 0.5), (20, 6.0)):
        params = with_stage(SetupParams(), CELL, "trial", now=start, budget_setups=n, budget_r=r)
        trial = params.trial_of("ES", "failed_break")
        assert trial is not None and (trial.budget_setups, trial.budget_r) == (n, r)
    for n, r in ((0, 3.0), (21, 3.0), (10, 0.49), (10, 6.01)):
        with pytest.raises(ValueError, match="mimo meze"):
            with_stage(SetupParams(), CELL, "trial", now=start, budget_setups=n, budget_r=r)


def test_parse_cell_pinovany_kontrakt() -> None:
    assert parse_cell("NQZ6:trend_continuation") == ("NQZ6", "trend_continuation")


def test_with_stage_prepina_a_obnova_zacina_od_nuly() -> None:
    first = dt.datetime(2026, 10, 1, 14, 5, tzinfo=dt.UTC)
    shadow = with_stage(SetupParams(), CELL, "shadow", now=first)
    assert shadow.stage_of("ES", "failed_break") == "shadow"
    trial = with_stage(shadow, CELL, "trial", now=first)
    assert trial.stage_of("ES", "failed_break") == "trial" and trial.shadow_cells == frozenset()
    started = trial.trial_of("ES", "failed_break")
    assert started is not None and started.started_at == first
    assert (started.budget_setups, started.budget_r) == (10, 3.0)  # výchozí rozpočet
    assert started.mechanics_version == SETUP_MECHANICS_VERSION  # razí server
    later = first + dt.timedelta(days=2)
    renewed = with_stage(trial, CELL, "trial", now=later, budget_setups=5, budget_r=2.0)
    again = renewed.trial_of("ES", "failed_break")
    assert again is not None and again.started_at == later and again.budget_setups == 5
    assert len(renewed.trial_cells) == 1
    assert with_stage(renewed, CELL, "auto", now=later) == SetupParams()


# ── Čerpání zkoušky (čistá funkce) ──────────────────────────────────


def _realized(
    outcome_r: float,
    created: dt.datetime,
    *,
    overridden: bool = True,
    template: str = "failed_break",
    symbol: str = "ES",
) -> RealizedSetup:
    return RealizedSetup(
        symbol=symbol,
        template=template,
        status="closed_stop" if outcome_r < 0 else "closed_target",
        outcome_r=outcome_r,
        closed_ts=created + dt.timedelta(minutes=30),
        tradeable=True,
        affordable=True,
        entry=7600.0,
        stop=7592.0,
        created_ts=created,
        gate_overridden=overridden,
    )


def test_cerpani_zkousky_vcetne_otevreneho_setupu() -> None:
    start = NOW - dt.timedelta(days=1)
    rows = [
        _realized(-1.0, start + dt.timedelta(hours=1)),
        _realized(2.0, start + dt.timedelta(hours=2)),
        # Nečerpá: před začátkem (dřívější zkouška), bez přebití brány, jiná buňka
        _realized(-1.0, start - dt.timedelta(minutes=1)),
        _realized(-1.0, start + dt.timedelta(hours=3), overridden=False),
        _realized(-1.0, start + dt.timedelta(hours=3), template="wall_bounce"),
        _realized(-1.0, start + dt.timedelta(hours=3), symbol="NQ"),
    ]
    kwargs: dict[str, Any] = {"started_at": start, "budget_setups": 3, "budget_r": 3.0}
    closed_only = trial_usage(rows, "ES", "failed_break", **kwargs)
    assert (closed_only.setups, closed_only.sum_r, closed_only.spent) == (2, 1.0, False)
    # Otevřený setup se počítá do počtu, R ještě nemá → 3/3 = vyčerpáno
    with_open = trial_usage(rows, "ES", "failed_break", open_setups=1, **kwargs)
    assert (with_open.setups, with_open.sum_r, with_open.spent) == (3, 1.0, True)
    # Ztráta: Σ R ≤ −budget_r vyčerpá i pod počtem setupů
    losses = [_realized(-1.0, start + dt.timedelta(hours=h)) for h in (1, 2, 3)]
    spent = trial_usage(
        losses, "ES", "failed_break", started_at=start, budget_setups=10, budget_r=3.0
    )
    assert (spent.setups, spent.sum_r, spent.spent) == (3, -3.0, True)


# ── Engine: priorita bloků a regrese Auto ───────────────────────────


class _Publisher(PublisherLike):
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def status(self, **fields: object) -> None:  # pragma: no cover
        pass

    async def publish(self, channel: str, data: dict[str, object]) -> None:
        self.events.append({"channel": channel, **data})

    def alerts(self, kind: str) -> list[dict[str, object]]:
        return [e for e in self.events if e["channel"] == "alerts" and e.get("kind") == kind]


def _engine(tmp_path: Path, symbol: str = "ES") -> tuple[SetupEngine, SetupsRepository, _Publisher]:
    repository = SetupsRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 's.sqlite'}"))
    repository.ensure_schema()
    oi_repo = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    oi_repo.ensure_schema()
    publisher = _Publisher()
    engine = SetupEngine(
        symbol=symbol, repository=repository, oi_repository=oi_repo, publisher=publisher
    )
    return engine, repository, publisher


def _history(repository: SetupsRepository, outcomes: list[float], *, closed: dt.datetime) -> None:
    """Uzavřené failed_break ES ve stínu brány (stop 8 b v rozpočtu)."""
    for outcome_r in outcomes:
        sid = repository.create(
            symbol="ES",
            expiry=closed.strftime("%Y%m%d"),
            template="failed_break",
            direction="long",
            created_ts=closed - dt.timedelta(minutes=30),
            entry=7600.0,
            target=7616.0,
            stop=7592.0,
            confidence=50,
            reason="historie",
            context={"affordable": True, "tradeable": False, "trade_block": "gate"},
        )
        repository.close(
            sid,
            status="closed_target" if outcome_r > 0 else "closed_stop",
            closed_ts=closed,
            outcome_r=outcome_r,
            mfe=1,
            mae=1,
        )


LAST_WEEK = dt.datetime(2026, 9, 8, 15, 0, tzinfo=dt.UTC)


def _risk(engine: SetupEngine, stop: float = 7592.0) -> dict[str, object]:
    realized = engine._load_realized(NOW)
    risk, _ = engine._risk_context(realized, "failed_break", 7600.0, stop, 50.0, NOW)
    return risk


def _daily_brake(repository: SetupsRepository) -> None:
    """Dnes −3 R obchodovatelných stopů jiné šablony na NQ → denní brzda."""
    for _ in range(3):
        sid = repository.create(
            symbol="NQ",
            expiry="20260916",
            template="trend_continuation",
            direction="short",
            created_ts=NOW - dt.timedelta(hours=1),
            entry=29000.0,
            target=28950.0,
            stop=29020.0,
            confidence=50,
            reason="dnes",
            context={"affordable": True, "tradeable": True},
        )
        repository.close(
            sid,
            status="closed_stop",
            closed_ts=NOW - dt.timedelta(minutes=30),
            outcome_r=-1.0,
            mfe=0,
            mae=1,
        )


TRIAL_KEYS = {
    "trial_started_at",
    "trial_budget_setups",
    "trial_budget_r",
    "trial_setups",
    "trial_sum_r",
}


@pytest.mark.parametrize("params", [SetupParams(), params_from_dict(PROD_V1)])
def test_auto_se_chova_jako_pred_1323(tmp_path: Path, params: SetupParams) -> None:
    """Regrese: buňka bez stádia (i verze 1 ze store) rozhoduje sizing → brzdy → brána."""
    engine, repository, _ = _engine(tmp_path)
    engine.apply_params(params, 1)
    # Brána pass: 35 setupů, 25 cílů po +1 R
    _history(repository, [1.0] * 25 + [-1.0] * 10, closed=LAST_WEEK)
    risk = _risk(engine)
    assert (risk["tradeable"], risk["trade_block"], risk["template_gate"]) == (True, None, "pass")
    assert risk["user_stage"] == "auto" and risk["gate_overridden"] is False
    assert not TRIAL_KEYS & set(risk)
    assert _risk(engine, stop=7586.0)["trade_block"] == "stop_over_budget"
    assert _risk(engine)["risk_rules_version"] == 3
    # Brána block (stejné řádky + 40 stopů) → stín „gate“, jako dřív
    _history(repository, [-1.0] * 40, closed=LAST_WEEK)
    blocked = _risk(engine)
    assert (blocked["tradeable"], blocked["trade_block"]) == (False, "gate")
    assert blocked["template_gate"] == "block" and blocked["gate_overridden"] is False
    # Denní brzda má přednost před bránou
    _daily_brake(repository)
    assert _risk(engine)["trade_block"] == "daily_brake"


def test_priorita_bloku_sizing_brzdy_user_brana(tmp_path: Path) -> None:
    engine, repository, _ = _engine(tmp_path)
    _history(repository, [1.0] * 25 + [-1.0] * 10, closed=LAST_WEEK)  # brána pass
    engine.apply_params(with_stage(SetupParams(), CELL, "shadow", now=NOW), 2)
    # Stín přebije i bránu pass; brána se zapíše dál
    shadow = _risk(engine)
    assert (shadow["tradeable"], shadow["trade_block"]) == (False, "user")
    assert shadow["template_gate"] == "pass" and shadow["user_stage"] == "shadow"
    # Sizing má přednost před stínem
    assert _risk(engine, stop=7586.0)["trade_block"] == "stop_over_budget"
    # Brzdy mají přednost před stínem (statistiky brzd srovnatelné s dneškem)
    _daily_brake(repository)
    assert _risk(engine)["trade_block"] == "daily_brake"
    # Jiná buňka téhož tickeru i týž setup na NQ zůstávají Auto
    other, _ = engine._risk_context(
        engine._load_realized(NOW), "wall_bounce", 7600.0, 7592.0, 50.0, NOW
    )
    assert other["user_stage"] == "auto"


def test_zkouska_prebije_jen_branu(tmp_path: Path) -> None:
    engine, repository, _ = _engine(tmp_path)
    engine.apply_params(
        with_stage(SetupParams(), CELL, "trial", now=NOW - dt.timedelta(hours=1)), 2
    )
    # Brána insufficient (bez historie) → zkouška pustí, verdikt zůstává zapsaný
    risk = _risk(engine)
    assert (risk["tradeable"], risk["trade_block"]) == (True, None)
    assert risk["template_gate"] == "insufficient" and risk["gate_overridden"] is True
    assert risk["user_stage"] == "trial" and risk["trial_setups"] == 1  # včetně tohoto
    assert (risk["trial_budget_setups"], risk["trial_budget_r"], risk["trial_sum_r"]) == (
        10,
        3.0,
        0.0,
    )
    # Brána block → zkouška pustí taky
    _history(repository, [-1.0] * 40, closed=LAST_WEEK)
    blocked = _risk(engine)
    assert blocked["template_gate"] == "block" and blocked["gate_overridden"] is True
    assert blocked["tradeable"] is True
    # Sizing ani brzdy zkouška nepřebije a rozpočet pak nečerpá
    over = _risk(engine, stop=7586.0)
    assert over["trade_block"] == "stop_over_budget" and over["gate_overridden"] is False
    assert over["trial_setups"] == 0
    _daily_brake(repository)
    braked = _risk(engine)
    assert braked["trade_block"] == "daily_brake" and braked["gate_overridden"] is False


def test_zkouska_pri_brane_pass_nic_neprebiji(tmp_path: Path) -> None:
    engine, repository, _ = _engine(tmp_path)
    _history(repository, [1.0] * 25 + [-1.0] * 10, closed=LAST_WEEK)
    engine.apply_params(
        with_stage(SetupParams(), CELL, "trial", now=NOW - dt.timedelta(hours=1)), 2
    )
    risk = _risk(engine)
    assert risk["tradeable"] is True and risk["gate_overridden"] is False
    assert risk["user_stage"] == "trial" and risk["trial_setups"] == 0


def test_zkouska_bez_overitelneho_cerpani_neprebiji(tmp_path: Path) -> None:
    """Čtení uzavřených setupů selhalo → čerpání nejde ověřit → brána platí."""
    engine, _, _ = _engine(tmp_path)
    engine.apply_params(
        with_stage(SetupParams(), CELL, "trial", now=NOW - dt.timedelta(hours=1)), 2
    )
    risk, _ = engine._risk_context([], "failed_break", 7600.0, 7592.0, 50.0, NOW, history_ok=False)
    assert (risk["tradeable"], risk["trade_block"]) == (False, "gate")
    assert risk["user_stage"] == "auto" and risk["gate_overridden"] is False
    assert not TRIAL_KEYS & set(risk)


# ── Engine: zkouška od vzniku po konec ──────────────────────────────


class _Runtime:
    expiry = "20991231"
    multiplier = 50.0
    last_profile = None


def _inputs(now: dt.datetime) -> MinuteInputs:
    return MinuteInputs(
        ts=now,
        open=7600.0,
        high=7600.0,
        low=7600.0,
        close=7600.0,
        flip=None,
        call_wall=None,
        put_wall=None,
        max_pain=None,
        cum_delta=0.0,
        call_flow=0.0,
        put_flow=0.0,
        opt_vol=0.0,
        minutes_to_expiry=None,
    )


CANDIDATE = SetupCandidate(
    template=SetupTemplate.FAILED_BREAK,
    direction=Direction.LONG,
    entry=7600.0,
    target=7616.0,
    stop=7592.0,  # 8 b × 50 $ = 400 $ → 1 kontrakt
    confidence=55,
    reason="test zkoušky",
    context={"gex_regime": "positive"},
)


async def _create(engine: SetupEngine, now: dt.datetime) -> None:
    entry_bar = Bar(now - dt.timedelta(minutes=1), 7600.0, 7600.0, 7600.0, 7600.0, 10.0)
    await engine._detect_new(now, cast(EngineRuntime, _Runtime()), _inputs(now), entry_bar)


async def _close(engine: SetupEngine, now: dt.datetime, *, win: bool) -> None:
    """Bar hned za barem vstupu zasáhne cíl, nebo stop."""
    bar_ts = now  # vstup = bar now − 1 min
    bar = (
        Bar(bar_ts, 7600.0, 7617.0, 7599.0, 7616.0, 10.0)
        if win
        else Bar(bar_ts, 7600.0, 7601.0, 7591.0, 7592.0, 10.0)
    )
    await engine._evaluate_open(now + dt.timedelta(minutes=1), [bar])


def _contexts(repository: SetupsRepository) -> list[dict[str, Any]]:
    rows = sorted(repository.list_for("ES"), key=lambda row: row["id"])
    return [row["context"] for row in rows]


@pytest.fixture
def _one_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setups_module, "detect_all", lambda history, params: [CANDIDATE])


@pytest.mark.usefixtures("_one_candidate")
async def test_zkouska_vycerpana_poctem_alert_jednou_pak_auto(tmp_path: Path) -> None:
    engine, repository, publisher = _engine(tmp_path)
    start = NOW - dt.timedelta(minutes=5)
    engine.apply_params(
        with_stage(SetupParams(), CELL, "trial", now=start, budget_setups=2, budget_r=3.0), 2
    )
    trial = engine.params.trial_of("ES", "failed_break")
    assert trial is not None

    await _create(engine, NOW)
    first = _contexts(repository)[-1]
    assert first["gate_overridden"] is True and first["trial_setups"] == 1
    # Otevřený setup zkoušky čerpá rozpočet hned (z paměti instance)
    usage = engine._trial_usage(engine._load_realized(NOW), trial)
    assert (usage.setups, usage.sum_r, usage.spent) == (1, 0.0, False)
    # Push nese stádium s čerpáním a řádek důkazu; Telegram filtr tradeable
    created = [a for a in publisher.alerts("setup") if a.get("event") == "created"][-1]
    assert created["tradeable"] is True and created["user_stage"] == "trial"
    assert created["gate_overridden"] is True and created["trial_setups"] == 1
    message = str(created["message"])
    assert "Nový setup LONG (T2 failed_break) · ZKOUŠKA 1/2 (+0.0 z -3.0 R):" in message
    # Zkouška pouští jen neprokázaný edge — push to říká štítkem
    assert "\nedge neprokázán: ØR — R (čistě —) · n 0 · brána insufficient" in message
    await _close(engine, NOW, win=True)

    # Druhý setup doplní počet → vyčerpáno při vzniku, alert hned a jednou
    second_ts = NOW + dt.timedelta(minutes=15)
    await _create(engine, second_ts)
    second = _contexts(repository)[-1]
    assert second["gate_overridden"] is True and second["trial_setups"] == 2
    spent = publisher.alerts("setup_stage")
    assert len(spent) == 1 and spent[0]["event"] == "trial_spent"
    assert spent[0]["symbol"] == "ES" and spent[0]["template"] == "failed_break"
    assert "Zkouška T2 failed_break · ES skončila: 2/2 setupů" in str(spent[0]["message"])
    assert "zpět na Auto" in str(spent[0]["message"])
    await _close(engine, second_ts, win=False)
    assert len(publisher.alerts("setup_stage")) == 1  # uzavření už nic neohlásí

    # Třetí kandidát: zkouška vyčerpaná = Auto → brána (n 2 < 30) → stín „gate“
    third_ts = NOW + dt.timedelta(minutes=30)
    await _create(engine, third_ts)
    third = _contexts(repository)[-1]
    assert (third["tradeable"], third["trade_block"]) == (False, "gate")
    assert third["user_stage"] == "auto" and third["gate_overridden"] is False
    assert third["trial_setups"] == 2 and third["trial_sum_r"] == pytest.approx(1.0)
    assert len(publisher.alerts("setup_stage")) == 1
    # Do parametrů se nic nezapsalo — stádium dál „trial“, rozhoduje čerpání
    assert engine.params.stage_of("ES", "failed_break") == "trial"


@pytest.mark.usefixtures("_one_candidate")
async def test_zkouska_vycerpana_ztratou_alert_pri_uzavreni(tmp_path: Path) -> None:
    engine, repository, publisher = _engine(tmp_path)
    engine.apply_params(
        with_stage(
            SetupParams(),
            CELL,
            "trial",
            now=NOW - dt.timedelta(minutes=5),
            budget_setups=10,
            budget_r=0.5,
        ),
        2,
    )
    await _create(engine, NOW)
    assert publisher.alerts("setup_stage") == []
    await _close(engine, NOW, win=False)  # −1 R ≤ −0,5 R
    spent = publisher.alerts("setup_stage")
    assert len(spent) == 1 and "1/10 setupů, -1.0 R z -0.5 R" in str(spent[0]["message"])
    await _create(engine, NOW + dt.timedelta(minutes=15))
    after = _contexts(repository)[-1]
    assert after["trade_block"] == "gate" and after["user_stage"] == "auto"
    assert len(publisher.alerts("setup_stage")) == 1


@pytest.mark.usefixtures("_one_candidate")
async def test_stin_bez_pushe_a_otevreny_setup_zkousky_po_restartu(tmp_path: Path) -> None:
    engine, repository, publisher = _engine(tmp_path)
    engine.apply_params(with_stage(SetupParams(), CELL, "shadow", now=NOW), 2)
    await _create(engine, NOW)
    shadow = [a for a in publisher.alerts("setup") if a.get("event") == "created"][-1]
    assert shadow["tradeable"] is False and shadow["user_stage"] == "shadow"
    assert "· STÍN:" in str(shadow["message"])
    assert "stín: ve stínu z rozhodnutí uživatele" in str(shadow["message"])
    await _close(engine, NOW, win=True)

    # Zkouška: setup zůstane otevřený, engine se restartuje
    start = NOW + dt.timedelta(minutes=10)
    params = with_stage(engine.params, CELL, "trial", now=start, budget_setups=1, budget_r=3.0)
    engine.apply_params(params, 3)
    await _create(engine, NOW + dt.timedelta(minutes=15))
    assert len(publisher.alerts("setup_stage")) == 1  # 1/1 při vzniku
    restarted, _, _ = _engine(tmp_path)
    restarted.apply_params(params, 3)
    assert [item.gate_overridden for item in restarted._open] == [True]
    trial = params.trial_of("ES", "failed_break")
    assert trial is not None
    usage = restarted._trial_usage(restarted._load_realized(NOW), trial)
    assert (usage.setups, usage.spent) == (1, True)
    # Setup ze stínu (před začátkem zkoušky) čerpání neovlivní
    assert usage.sum_r == 0.0


def test_realizovane_nesou_vznik_a_prebiti_brany(tmp_path: Path) -> None:
    _, repository, _ = _engine(tmp_path)
    sid = repository.create(
        symbol="ES",
        expiry="20260916",
        template="failed_break",
        direction="long",
        created_ts=NOW - dt.timedelta(hours=1),
        entry=7600.0,
        target=7616.0,
        stop=7592.0,
        confidence=50,
        reason="zkouška",
        context={"affordable": True, "tradeable": True, "gate_overridden": True},
    )
    assert [s.gate_overridden for s in repository.active_for("ES")] == [True]
    repository.close(
        sid,
        status="closed_stop",
        closed_ts=NOW - dt.timedelta(minutes=5),
        outcome_r=-1.0,
        mfe=0,
        mae=1,
    )
    (row,) = repository.realized_since(NOW - dt.timedelta(days=1), mechanics_version=5)
    assert row.gate_overridden is True
    assert row.created_ts == NOW - dt.timedelta(hours=1)


@pytest.mark.usefixtures("_one_candidate")
async def test_zkouska_plati_az_od_zacatku_cerpani_nic_neunikne(tmp_path: Path) -> None:
    """Review #1323: cyklus enginu má `now` zaokrouhlené na minutu, API razí
    začátek zkoušky přesně. Setup z cyklu, ve kterém se zkouška aplikovala
    (14:00:00 < začátek 14:00:20), ji nesmí přebít bránu — jinak by ho zkouška
    pustila, ale čerpání (setupy od `started_at`) by ho nikdy nezapočetlo."""
    engine, repository, publisher = _engine(tmp_path)
    started = NOW + dt.timedelta(seconds=20)
    engine.apply_params(
        with_stage(SetupParams(), CELL, "trial", now=started, budget_setups=1, budget_r=0.5), 2
    )
    await _create(engine, NOW)
    early = _contexts(repository)[-1]
    assert (early["tradeable"], early["trade_block"]) == (False, "gate")
    assert early["user_stage"] == "auto" and early["gate_overridden"] is False
    assert not TRIAL_KEYS & set(early)
    await _close(engine, NOW, win=False)
    assert publisher.alerts("setup_stage") == []

    # Příští cyklus už zkouška platí: setup čerpá a 1/1 ji hned vyčerpá
    second_ts = NOW + dt.timedelta(minutes=15)
    await _create(engine, second_ts)
    second = _contexts(repository)[-1]
    assert second["gate_overridden"] is True and second["trial_setups"] == 1
    assert len(publisher.alerts("setup_stage")) == 1
    await _close(engine, second_ts, win=False)
    trial = engine.params.trial_of("ES", "failed_break")
    assert trial is not None
    usage = engine._trial_usage(engine._load_realized(second_ts + dt.timedelta(minutes=2)), trial)
    assert (usage.setups, usage.sum_r, usage.spent) == (1, -1.0, True)
    # Vyčerpaná zkouška už nepustí nic
    await _create(engine, NOW + dt.timedelta(minutes=30))
    third = _contexts(repository)[-1]
    assert third["gate_overridden"] is False and third["trade_block"] == "gate"
    assert len(publisher.alerts("setup_stage")) == 1


def test_zkouska_jine_mechaniky_skoncila(tmp_path: Path) -> None:
    """Čerpání se čte jen z aktuální mechaniky: po zvednutí verze by vyčerpaná
    zkouška dostala plný rozpočet bez rozhodnutí uživatele — proto končí (Auto)."""
    engine, _, _ = _engine(tmp_path)
    params = with_stage(SetupParams(), CELL, "trial", now=NOW - dt.timedelta(hours=1))
    (trial,) = params.trial_cells
    stale = replace(trial, mechanics_version=SETUP_MECHANICS_VERSION - 1)
    old = replace(params, trial_cells=(stale,))
    engine.apply_params(old, 2)
    risk = _risk(engine)
    assert (risk["tradeable"], risk["trade_block"]) == (False, "gate")
    assert risk["user_stage"] == "auto" and risk["gate_overridden"] is False
    assert not TRIAL_KEYS & set(risk)
    # Nastavené stádium zůstává (nic se nezapsalo); obnovit jde jen novou verzí
    assert engine.params.stage_of("ES", "failed_break") == "trial"
    engine.apply_params(params, 3)
    assert _risk(engine)["gate_overridden"] is True


def test_radek_dukazu_stitek_edge() -> None:
    blocked: dict[str, object] = {
        "template_gate": "block",
        "template_gate_n": 163,
        "template_gate_lb": -0.14,
        "template_gate_avg_r": 0.09,
        "template_gate_avg_net_r": 0.03,
        "template_gate_n_needed": 462,
    }
    assert setups_module.evidence_line(blocked) == (
        "edge neprokázán: ØR +0.09 R (čistě +0.03) · n 163/462 · brána block (LB -0.14)"
    )
    passed = {**blocked, "template_gate": "pass", "template_gate_lb": 0.05}
    assert setups_module.evidence_line(passed).startswith("Důkaz: ØR +0.09 R")


def test_navrh_walk_forwardu_nenese_stadia() -> None:
    """Stádia mění jen POST /setups/stage; návrh se stádii by po změně stádia
    API odmítlo (422)."""
    script = Path(__file__).resolve().parents[2] / "scripts" / "walkforward_setups.py"
    spec = importlib.util.spec_from_file_location("walkforward_setups", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    staged = with_stage(SetupParams(), CELL, "shadow", now=NOW)
    result = WalkForwardResult(baseline="baseline", candidates=("x",), folds=(), proposal="x")
    payload = module.proposal_payload(result, {"x": staged}, "ES")
    assert payload is not None
    assert "shadow_cells" not in payload["params"] and "trial_cells" not in payload["params"]
    assert payload["params"]["min_rrr"] == staged.min_rrr
