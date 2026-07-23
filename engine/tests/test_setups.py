"""Testy setup detektoru (ADR-0004): šablony T1–T4, vyhodnocení, orchestrace."""

import datetime as dt
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.levels import GexLevels
from gexlens_engine.compute.setups import (
    Direction,
    MinuteInputs,
    Outcome,
    SetupParams,
    SetupTemplate,
    detect_failed_break,
    detect_gamma_momentum,
    detect_max_pain_pin,
    detect_wall_bounce,
    evaluate_bar,
    max_pain_strike,
    r_result,
)
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.setups import SetupEngine
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.parquet_store import LevelsRow
from gexlens_engine.storage.setups_store import SetupsRepository

TS = dt.datetime(2026, 7, 17, 15, 0, tzinfo=dt.UTC)
PARAMS = SetupParams()


def minute(
    close: float,
    *,
    low: float | None = None,
    high: float | None = None,
    flip: float | None = 7515.0,
    call_wall: float | None = 7530.0,
    put_wall: float | None = 7500.0,
    max_pain: float | None = None,
    cum_delta: float = 0.0,
    call_flow: float = 0.0,
    put_flow: float = 0.0,
    opt_vol: float = 10.0,
    minutes_to_expiry: float | None = 600.0,
    call_wall_dom: float | None = None,
    put_wall_dom: float | None = None,
    idx: int = 0,
) -> MinuteInputs:
    return MinuteInputs(
        ts=TS + dt.timedelta(minutes=idx),
        open=close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        flip=flip,
        call_wall=call_wall,
        put_wall=put_wall,
        max_pain=max_pain,
        cum_delta=cum_delta,
        call_flow=call_flow,
        put_flow=put_flow,
        opt_vol=opt_vol,
        minutes_to_expiry=minutes_to_expiry,
        call_wall_dom=call_wall_dom,
        put_wall_dom=put_wall_dom,
    )


# ── Max Pain ───────────────────────────────────────────────────────


def test_max_pain_strike_symmetric() -> None:
    oi = {(7490.0, "C"): 10.0, (7500.0, "C"): 10.0, (7510.0, "C"): 10.0}
    oi |= {(7490.0, "P"): 10.0, (7500.0, "P"): 10.0, (7510.0, "P"): 10.0}
    assert max_pain_strike(oi) == 7500.0
    assert max_pain_strike({}) is None


# ── T1: odraz od zdi ───────────────────────────────────────────────


def test_wall_bounce_long_at_put_wall() -> None:
    # Cena 10 minut klesá k put zdi 7500, Cum Δ přitom roste (divergence),
    # poslední minuta sáhne do zóny (low 7501) a zavře nad zdí (7502).
    history = [minute(7512 - i, cum_delta=float(i * 10), idx=i) for i in range(10)]
    history.append(minute(7502, low=7501, cum_delta=110.0, idx=10))

    setup = detect_wall_bounce(history, PARAMS)
    assert setup is not None
    assert setup.template is SetupTemplate.WALL_BOUNCE
    assert setup.direction is Direction.LONG
    assert setup.entry == 7502
    assert setup.target == 7515  # nejbližší úroveň nad entry = flip
    # buffer = max(3, 0.25×13) = 3.25 → stop 7496.75; RRR = 13/5.25 ≈ 2.48
    assert setup.stop == pytest.approx(7496.75)
    assert setup.rrr == pytest.approx(13 / 5.25, rel=1e-3)
    # close 7502 < flip 7515 → cena pod flipem = špatná gamma strana → 45
    assert setup.confidence == 45
    assert "špatné straně flipu" in setup.reason


def test_wall_bounce_right_gamma_side_full_confidence() -> None:
    # Flip pod cenou (7495) → close nad flipem = správná strana → 55;
    # cíl je pak call wall 7530 (jediná úroveň nad entry)
    history = [minute(7512 - i, flip=7495.0, cum_delta=float(i * 10), idx=i) for i in range(10)]
    history.append(minute(7502, low=7501, flip=7495.0, cum_delta=110.0, idx=10))
    setup = detect_wall_bounce(history, PARAMS)
    assert setup is not None
    assert setup.confidence == 55
    assert setup.target == 7530


def test_wall_bounce_requires_wall_dominance() -> None:  # ADR-0010, #223
    """Slabá zeď (dominance pod prahem) = argmax nad plochým profilem → žádný T1."""

    def history_with(dom: float | None) -> list[MinuteInputs]:
        rows = [
            minute(7512 - i, cum_delta=float(i * 10), put_wall_dom=dom, idx=i) for i in range(10)
        ]
        rows.append(minute(7502, low=7501, cum_delta=110.0, put_wall_dom=dom, idx=10))
        return rows

    assert detect_wall_bounce(history_with(0.05), PARAMS) is None  # pod prahem 0.15
    strong = detect_wall_bounce(history_with(0.4), PARAMS)
    assert strong is not None
    assert strong.context["wall_dom"] == 0.4
    # None = dominance neznámá (starší data) → podmínka se přeskakuje
    assert detect_wall_bounce(history_with(None), PARAMS) is not None


def test_wall_bounce_discards_low_rrr() -> None:
    # Cíl (flip) těsně nad entry → RRR < 1.2 → žádný setup
    history = [minute(7512 - i, flip=7503.5, cum_delta=float(i * 10), idx=i) for i in range(10)]
    history.append(minute(7502, low=7501, flip=7503.5, cum_delta=110.0, idx=10))
    assert detect_wall_bounce(history, PARAMS) is None


# ── T2: neúspěšný průraz (páteční scénář 7500 → 7473 → reclaim) ────


def failed_break_history() -> list[MinuteInputs]:
    return [
        minute(7505, idx=0),
        minute(7496, low=7473, idx=1),  # průraz 7500 − 3 s dnem 7473
        minute(7501, low=7495, idx=2),  # čerstvý reclaim ≥ 7501
    ]


def test_failed_breakdown_reclaim_long() -> None:
    setup = detect_failed_break(failed_break_history(), PARAMS)
    assert setup is not None
    assert setup.template is SetupTemplate.FAILED_BREAK
    assert setup.direction is Direction.LONG
    assert setup.entry == 7501
    assert setup.stop == 7472  # extrém 7473 − 1
    assert setup.target == 7515


def test_failed_breakdown_dies_on_acceptance() -> None:
    history = [minute(7505, idx=0), minute(7496, low=7473, idx=1)]
    # 5 po sobě jdoucích closes pod 7500 = akceptace → šablona mrtvá
    for i in range(5):
        history.append(minute(7495 + i * 0.1, idx=2 + i))
    history.append(minute(7501, idx=7))
    assert detect_failed_break(history, PARAMS) is None


# ── T3: Max Pain pin ───────────────────────────────────────────────


def test_max_pain_pin_short_above() -> None:
    history = [
        minute(7520, max_pain=7510.0, opt_vol=10.0, minutes_to_expiry=700 - i, idx=i)
        for i in range(80)
    ]
    for i in range(30):  # aktivita vyhasíná
        history.append(
            minute(7520, max_pain=7510.0, opt_vol=1.0, minutes_to_expiry=120.0 - i, idx=80 + i)
        )
    setup = detect_max_pain_pin(history, PARAMS)
    assert setup is not None
    assert setup.direction is Direction.SHORT
    assert setup.target == 7510
    assert setup.stop == pytest.approx(7535)  # 1.5 × 10 nad entry


def test_max_pain_pin_requires_distance_and_time() -> None:
    close_to_mp = [minute(7512, max_pain=7510.0, minutes_to_expiry=100.0, idx=0)]
    assert detect_max_pain_pin(close_to_mp, PARAMS) is None
    too_early = [minute(7530, max_pain=7510.0, minutes_to_expiry=500.0, idx=0)]
    assert detect_max_pain_pin(too_early, PARAMS) is None


def test_max_pain_pin_requires_positioning_concentration() -> None:  # ADR-0010, #223
    """Pin bez dominantní zdi (plochý profil) magnet netvoří → žádný T3."""

    def pin_minute(call_dom: float | None, put_dom: float | None) -> list[MinuteInputs]:
        return [
            minute(
                7530,
                max_pain=7510.0,
                minutes_to_expiry=100.0,
                call_wall_dom=call_dom,
                put_wall_dom=put_dom,
                idx=0,
            )
        ]

    assert detect_max_pain_pin(pin_minute(0.05, 0.08), PARAMS) is None  # obě pod prahem
    strong = detect_max_pain_pin(pin_minute(0.05, 0.4), PARAMS)  # stačí jedna strana
    assert strong is not None
    assert strong.context["wall_dom_max"] == 0.4
    # Neznámé dominance (obě None) podmínku přeskakují
    assert detect_max_pain_pin(pin_minute(None, None), PARAMS) is not None


# ── T4: gamma momentum ─────────────────────────────────────────────


def test_gamma_momentum_short_on_flip_break() -> None:
    history = [
        minute(7516, cum_delta=-float(i), put_flow=7.0, call_flow=3.0, idx=i) for i in range(12)
    ]
    history.append(
        minute(7512, cum_delta=-50.0, put_flow=7.0, call_flow=3.0, idx=12)
    )  # close ≤ flip − 2, Cum Δ nové minimum
    setup = detect_gamma_momentum(history, PARAMS)
    assert setup is not None
    assert setup.direction is Direction.SHORT
    assert setup.target == 7500  # put wall
    assert setup.stop == 7516  # flip + 1


def test_gamma_momentum_needs_flow_share() -> None:
    history = [
        minute(7516, cum_delta=-float(i), put_flow=5.0, call_flow=5.0, idx=i) for i in range(12)
    ]
    history.append(minute(7512, cum_delta=-50.0, put_flow=5.0, call_flow=5.0, idx=12))
    assert detect_gamma_momentum(history, PARAMS) is None  # 50 % < 60 %


# ── Vyhodnocení ────────────────────────────────────────────────────


def test_evaluate_bar_conservative_stop_first() -> None:
    # Bar zasáhl stop i cíl → konzervativně stop
    assert evaluate_bar(Direction.LONG, 7501, 7515, 7472, high=7520, low=7470) is Outcome.STOP
    assert evaluate_bar(Direction.LONG, 7501, 7515, 7472, high=7516, low=7490) is Outcome.TARGET
    assert evaluate_bar(Direction.SHORT, 7520, 7510, 7535, high=7536, low=7508) is Outcome.STOP
    assert evaluate_bar(Direction.LONG, 7501, 7515, 7472, high=7510, low=7490) is None


def test_r_result() -> None:
    assert r_result(Direction.LONG, 7501, 7472, 7515) == pytest.approx(14 / 29)
    assert r_result(Direction.LONG, 7501, 7472, 7472) == pytest.approx(-1.0)
    assert r_result(Direction.SHORT, 7520, 7535, 7510) == pytest.approx(10 / 15)


# ── SetupEngine orchestrace (sqlite) ───────────────────────────────


class RecordingPublisher(PublisherLike):
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []

    async def status(self, **fields: object) -> None:  # pragma: no cover
        pass

    async def publish(self, channel: str, data: dict[str, object]) -> None:
        self.messages.append((channel, data))


class FakeScheduler:
    def quotes(self) -> dict[object, object]:
        return {}


class FakeFlow:
    def __init__(self, cum_delta: float) -> None:
        self.ts_min = TS
        self.flow_delta = 0.0
        self.cum_delta = cum_delta


class FakeRuntime:
    def __init__(self) -> None:
        self.expiry = "20991231"  # daleko — žádný timeout
        self.scheduler = FakeScheduler()
        self.last_levels = LevelsRow(TS, 7515.0, 7530.0, 7500.0, 7512.0, 100.0)
        self.last_flow = FakeFlow(0.0)
        # Dominance zdí (ADR-0010, #223) — SetupEngine je čte z plných levels
        self.last_gex_levels = GexLevels(
            flip=7515.0,
            call_wall=7530.0,
            put_wall=7500.0,
            centroid=7512.0,
            total_gex=100.0,
            call_wall_dom=0.5,
            put_wall_dom=0.5,
        )


async def test_setup_engine_end_to_end(tmp_path: Path) -> None:
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}")
    repository = SetupsRepository(db)
    repository.ensure_schema()
    oi_repo = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    oi_repo.ensure_schema()
    publisher = RecordingPublisher()
    runtime = cast(EngineRuntime, FakeRuntime())
    engine = SetupEngine(
        symbol="ES", repository=repository, oi_repository=oi_repo, publisher=publisher
    )

    def bar(o: float, h: float, low: float, c: float) -> Bar:
        return Bar(ts=TS, open=o, high=h, low=low, close=c, volume=100.0)

    # Páteční scénář: baseline → průraz 7500 s dnem 7473 → reclaim 7501 → setup LONG
    await engine.on_minute(TS, 7505, [bar(7505, 7506, 7504, 7505)], runtime)
    await engine.on_minute(
        TS + dt.timedelta(minutes=1), 7496, [bar(7500, 7500, 7473, 7496)], runtime
    )
    await engine.on_minute(
        TS + dt.timedelta(minutes=2), 7501, [bar(7496, 7502, 7495, 7501)], runtime
    )

    active = repository.active_for("ES")
    assert len(active) == 1
    assert active[0].template == "failed_break"
    assert active[0].direction == "long"
    assert active[0].stop == 7472

    created_alerts = [d for ch, d in publisher.messages if ch == "alerts"]
    assert any("Nový setup LONG" in str(a["message"]) for a in created_alerts)
    # Proklik ve zvonečku (#186): nový setup nese event=created
    assert any(a.get("event") == "created" for a in created_alerts)
    assert any(ch == "setups.ES" for ch, _ in publisher.messages)

    # Další minuta zasáhne cíl 7515 → closed_target s kladným R
    await engine.on_minute(
        TS + dt.timedelta(minutes=3), 7516, [bar(7501, 7516, 7500, 7516)], runtime
    )
    assert repository.active_for("ES") == []
    # Výsledek nese event=closed (#186)
    closed_alerts = [d for ch, d in publisher.messages if ch == "alerts"]
    assert any(a.get("event") == "closed" and "uzavřen" in str(a["message"]) for a in closed_alerts)
    rows = repository.list_for("ES")
    assert rows[0]["status"] == "closed_target"
    assert rows[0]["outcome_r"] == pytest.approx(14 / 29, rel=1e-3)
    assert rows[0]["mfe"] >= 14

    # Ruční hodnocení (jediná mutace po uzavření)
    assert repository.review(rows[0]["id"], 1, "vyšlo přesně podle predikce")
    reviewed = repository.list_for("ES")[0]
    assert reviewed["user_rating"] == 1
    assert reviewed["user_note"] == "vyšlo přesně podle predikce"
