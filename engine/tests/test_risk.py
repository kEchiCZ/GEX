"""Risk framework malého účtu (#1185 A): sizing, brzdy, brána šablon, kontext setupu."""

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.risk import (
    RealizedSetup,
    affordable_results,
    brake_state,
    expectancy_lower_bound,
    position_size,
    template_gate,
    week_start,
)
from gexlens_engine.compute.settle import session_bounds
from gexlens_engine.compute.setups import SetupParams, params_from_dict, params_to_dict
from gexlens_engine.runtime import PublisherLike
from gexlens_engine.setups import SetupEngine
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.setups_store import SetupsRepository

SESSION = dt.date(2026, 9, 16)  # středa
OPEN = session_bounds(SESSION)[0]


def test_sizing_ucet_50k_jedno_procento() -> None:
    # ES 10 b × 50 $ = 500 $ = přesně rozpočet → 1 kontrakt
    size = position_size(7600.0, 7590.0, 50.0, account_equity_usd=50000, risk_pct=1, risk_max_pct=2)
    assert size.contracts == 1 and size.affordable and size.max_loss_usd == 500.0
    # NQ 25 b × 20 $ = 500 $ → 1 kontrakt; 26 b → 0 → stop nad rozpočtem
    assert (
        position_size(
            29000, 28975, 20.0, account_equity_usd=50000, risk_pct=1, risk_max_pct=2
        ).contracts
        == 1
    )
    over = position_size(29000, 28974, 20.0, account_equity_usd=50000, risk_pct=1, risk_max_pct=2)
    assert not over.affordable and over.block == "stop_over_budget" and over.contracts == 0
    # Těsný stop = víc kontraktů (ES 4 b → ⌊500/200⌋ = 2, ztráta 400 $)
    tight = position_size(
        7600.0, 7596.0, 50.0, account_equity_usd=50000, risk_pct=1, risk_max_pct=2
    )
    assert tight.contracts == 2 and tight.max_loss_usd == 400.0
    # Tvrdý strop: risk_pct zvednutý nad risk_max_pct nesmí pustit ztrátu nad 2 %
    capped = position_size(
        7600.0, 7575.0, 50.0, account_equity_usd=50000, risk_pct=3, risk_max_pct=2
    )
    assert capped.block == "stop_over_cap" and not capped.affordable
    # Stop 0 b / záporný účet = neobchodovatelné, ne výjimka
    assert (
        position_size(
            7600.0, 7600.0, 50.0, account_equity_usd=50000, risk_pct=1, risk_max_pct=2
        ).contracts
        == 0
    )
    assert (
        position_size(
            7600.0, 7590.0, 50.0, account_equity_usd=0, risk_pct=1, risk_max_pct=2
        ).contracts
        == 0
    )


def _row(
    template: str,
    outcome_r: float,
    closed: dt.datetime,
    *,
    tradeable: bool | None = True,
    status: str | None = None,
    symbol: str = "ES",
    entry: float = 7600.0,
    stop: float = 7590.0,
    affordable: bool | None = None,
) -> RealizedSetup:
    return RealizedSetup(
        symbol=symbol,
        template=template,
        status=status or ("closed_stop" if outcome_r < 0 else "closed_target"),
        outcome_r=outcome_r,
        closed_ts=closed,
        tradeable=tradeable,
        affordable=affordable,
        entry=entry,
        stop=stop,
    )


def test_brzdy_den_tyden_a_sablona() -> None:
    kwargs: dict[str, Any] = {
        "session_day": SESSION,
        "daily_brake_r": 3.0,
        "weekly_brake_r": 6.0,
        "max_template_stops_per_day": 2,
    }
    t = OPEN + dt.timedelta(hours=16)
    # Dvě stopy dnes (−2 R) → nic; třetí (−3 R) → denní brzda
    two = [_row("failed_break", -1.0, t), _row("wall_bounce", -1.0, t)]
    assert brake_state(two, "failed_break", **kwargs).block is None
    three = [*two, _row("trend_continuation", -1.0, t)]
    state = brake_state(three, "failed_break", **kwargs)
    assert state.block == "daily_brake" and state.day_r == -3.0
    # Řádky před pravidly (tradeable None) a stínové (False) se nepočítají
    ignored = [
        _row("failed_break", -5.0, t, tradeable=None),
        _row("failed_break", -5.0, t, tradeable=False),
    ]
    assert brake_state(ignored, "failed_break", **kwargs).block is None
    # Týden: −6 R rozprostřených od pondělí, dnes jen −1 R → týdenní brzda
    monday = week_start(SESSION) + dt.timedelta(hours=20)
    week = [
        _row("wall_bounce", -2.5, monday),
        _row("wall_bounce", -2.5, monday + dt.timedelta(days=1)),
        _row("wall_bounce", -1.0, t),
    ]
    state = brake_state(week, "wall_bounce", **kwargs)
    assert state.block == "weekly_brake" and state.week_r == -6.0 and state.day_r == -1.0
    # Dva stopy téže šablony dnes (+ výhra jinde, den v plusu) → strop šablony jen pro ni
    stops = [
        _row("max_pain_pin", -1.0, t),
        _row("max_pain_pin", -1.0, t),
        _row("failed_break", 3.0, t),
    ]
    assert brake_state(stops, "max_pain_pin", **kwargs).block == "template_stops"
    assert brake_state(stops, "failed_break", **kwargs).block is None
    # Vypnuté brzdy (0) nikdy neblokují
    off = brake_state(
        three,
        "failed_break",
        session_day=SESSION,
        daily_brake_r=0,
        weekly_brake_r=0,
        max_template_stops_per_day=0,
    )
    assert off.block is None


def test_brana_sablony_dolni_mez_ocekavani() -> None:
    assert expectancy_lower_bound([1.0]) is None
    # 40 výsledků Ø +0,5 R s malým rozptylem → LB > 0 → pass
    good = [0.4, 0.6] * 20
    assert template_gate(good, min_samples=30, enabled=True).verdict == "pass"
    # Ø +0,03 R s rozptylem stopů a cílů (v5 realita) → LB < 0 → block
    real = [2.0] * 8 + [-1.0] * 22 + [0.3] * 10
    gate = template_gate(real, min_samples=30, enabled=True)
    assert gate.verdict == "block" and gate.lower_bound is not None and gate.lower_bound < 0
    assert template_gate(good[:20], min_samples=30, enabled=True).verdict == "insufficient"
    assert template_gate(real, min_samples=30, enabled=False).verdict == "off"


def test_brana_pocita_jen_zobchodovatelne_a_dopocitava_stare_radky() -> None:
    t = OPEN + dt.timedelta(hours=16)
    rows = [
        _row("failed_break", 2.0, t, affordable=True),  # nový řádek, v rozpočtu
        _row("failed_break", -1.0, t, affordable=False),  # nový řádek, stop nad rozpočtem
        _row("failed_break", 1.0, t, tradeable=None, stop=7590.0),  # starý, 10 b ES → v rozpočtu
        _row("failed_break", -1.0, t, tradeable=None, stop=7560.0),  # starý, 40 b → mimo
        _row(
            "failed_break", 1.5, t, tradeable=None, symbol="NQ"
        ),  # cizí symbol bez hodnoty bodu → vynechat
        _row("wall_bounce", 5.0, t, affordable=True),  # jiná šablona
        _row("failed_break", 9.0, t - dt.timedelta(days=100), affordable=True),  # mimo okno
    ]
    results = affordable_results(
        rows,
        "failed_break",
        since=t - dt.timedelta(days=84),
        point_values={"ES": 50.0},
        account_equity_usd=50000,
        risk_pct=1,
        risk_max_pct=2,
    )
    assert results == [2.0, 1.0]


def test_risk_parametry_jsou_ve_store() -> None:
    params = params_to_dict(SetupParams())
    assert params["account_equity_usd"] == 50000.0 and params["risk_pct"] == 1.0
    assert params["template_gate_enabled"] is True
    loaded = params_from_dict({"risk_pct": 2, "template_gate_enabled": False})
    assert loaded.risk_pct == 2.0 and loaded.template_gate_enabled is False
    with pytest.raises(ValueError):
        params_from_dict({"template_gate_enabled": 1})


class _Publisher(PublisherLike):
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def status(self, **fields: object) -> None:  # pragma: no cover
        pass

    async def publish(self, channel: str, data: dict[str, object]) -> None:
        self.events.append({"channel": channel, **data})


async def test_setup_engine_zapisuje_risk_kontext_a_brzdu(tmp_path: Path) -> None:
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}")
    repository = SetupsRepository(db)
    repository.ensure_schema()
    oi_repo = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    oi_repo.ensure_schema()
    publisher = _Publisher()
    engine = SetupEngine(
        symbol="ES", repository=repository, oi_repository=oi_repo, publisher=publisher
    )
    engine.point_values["ES"] = 50.0
    now = OPEN + dt.timedelta(hours=16)
    # Track record: 35 failed_break v rozpočtu s kladnou dolní mezí → brána pass
    for i in range(35):
        sid = repository.create(
            symbol="ES",
            expiry="20260915",
            template="failed_break",
            direction="long",
            created_ts=now - dt.timedelta(days=2),
            entry=7600.0,
            target=7620.0,
            stop=7592.0,
            confidence=50,
            reason="historie",
            context={"affordable": True, "tradeable": True},
        )
        repository.close(
            sid,
            status="closed_target" if i < 25 else "closed_stop",
            closed_ts=now - dt.timedelta(days=2),
            outcome_r=1.0 if i < 25 else -1.0,
            mfe=1,
            mae=0,
        )
    realized = engine._load_realized(now)
    assert len(realized) == 35
    # Kandidát v rozpočtu: 8 b ES → 1 kontrakt, 400 $, obchodovatelný
    risk, brakes = engine._risk_context(realized, "failed_break", 7600.0, 7592.0, 50.0, now)
    assert risk["tradeable"] is True and risk["contracts"] == 1 and risk["max_loss_usd"] == 400.0
    assert risk["template_gate"] == "pass" and risk["trade_block"] is None and brakes.block is None
    # Stop 14 b → stín (stop nad rozpočtem), brána se pořád zapíše
    risk, _ = engine._risk_context(realized, "failed_break", 7600.0, 7586.0, 50.0, now)
    assert risk["tradeable"] is False and risk["trade_block"] == "stop_over_budget"
    # Šablona bez vzorku → gate insufficient → stín
    risk, _ = engine._risk_context(realized, "wall_bounce", 7600.0, 7592.0, 50.0, now)
    assert risk["trade_block"] == "gate" and risk["template_gate"] == "insufficient"
    # Dnes −3 R obchodovatelných → denní brzda, alert jednou za seanci
    for _ in range(3):
        sid = repository.create(
            symbol="NQ",
            expiry="20260916",
            template="trend_continuation",
            direction="short",
            created_ts=now - dt.timedelta(hours=1),
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
            closed_ts=now - dt.timedelta(minutes=30),
            outcome_r=-1.0,
            mfe=0,
            mae=1,
        )
    realized = engine._load_realized(now)
    risk, brakes = engine._risk_context(realized, "failed_break", 7600.0, 7592.0, 50.0, now)
    assert risk["trade_block"] == "daily_brake" and risk["realized_day_r"] == -3.0
    await engine._alert_brake(brakes, now)
    await engine._alert_brake(brakes, now + dt.timedelta(minutes=5))
    brake_alerts = [e for e in publisher.events if e.get("kind") == "risk_brake"]
    assert len(brake_alerts) == 1 and brake_alerts[0]["event"] == "daily_brake"
    assert "−3.0 R" in brake_alerts[0]["message"].replace("-", "−")
