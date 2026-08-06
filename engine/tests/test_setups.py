"""Testy setup detektoru (ADR-0004): šablony T1–T4, vyhodnocení, orchestrace."""

import datetime as dt
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine, text

from gexlens_engine.compute.levels import GexLevels
from gexlens_engine.compute.setups import (
    SETUP_MECHANICS_VERSION,
    Direction,
    MinuteInputs,
    Outcome,
    SetupCandidate,
    SetupParams,
    SetupTemplate,
    _ema,
    average_true_range,
    detect_all,
    detect_divergence_spring,
    detect_failed_break,
    detect_gamma_momentum,
    detect_max_pain_pin,
    detect_trend_continuation,
    detect_wall_bounce,
    evaluate_bar,
    gex_regime,
    is_counter_regime,
    max_pain_strike,
    normalize_candidate,
    r_result,
    scale_params,
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
    gex_regime: str | None = None,
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
        gex_regime=gex_regime,
    )


# ── Max Pain ───────────────────────────────────────────────────────


def test_max_pain_strike_symmetric() -> None:
    oi = {(7490.0, "C"): 10.0, (7500.0, "C"): 10.0, (7510.0, "C"): 10.0}
    oi |= {(7490.0, "P"): 10.0, (7500.0, "P"): 10.0, (7510.0, "P"): 10.0}
    assert max_pain_strike(oi) == 7500.0
    assert max_pain_strike({}) is None


# ── GEX režim (#209) ──────────────────────────────────────────────


def test_gex_regime_from_flip_and_total_gex() -> None:
    assert gex_regime(7520.0, 7515.0, -100.0) == "positive"  # close nad flipem
    assert gex_regime(7510.0, 7515.0, 100.0) == "negative"  # close pod flipem
    assert gex_regime(7515.0, 7515.0, 0.0) == "positive"  # na flipu = pozitivní strana (T1)
    assert gex_regime(7515.0, None, 100.0) == "positive"  # bez flipu → znaménko TotalGEX
    assert gex_regime(7515.0, None, -100.0) == "negative"
    assert gex_regime(7515.0, None, 0.0) is None


def test_detectors_carry_gex_regime_in_context() -> None:
    # T1 s režimem: kontext ho nese pro kalibraci Fáze 2 (#209). Kontra-režim
    # (long v negativní gammě) potřebuje CumΔ konfluenci přes 30 min (#252 B)
    # → historie s rostoucí CumΔ přes celé okno
    history = [minute(7513, cum_delta=0.0, gex_regime="negative", idx=i) for i in range(21)]
    history += [
        minute(7512 - i, cum_delta=float(i * 10), gex_regime="negative", idx=21 + i)
        for i in range(10)
    ]
    history.append(minute(7502, low=7501, cum_delta=110.0, gex_regime="negative", idx=31))
    setup = detect_wall_bounce(history, PARAMS)
    assert setup is not None
    assert setup.context["gex_regime"] == "negative"
    assert setup.context["counter_regime"] is True
    assert "Kontra-režim potvrzen tokem" in setup.reason


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
    # Cíl (flip) těsně nad entry → RRR < 1.2. Od #302 RRR neřeší jednotlivé
    # šablony (zapomínalo se na to), ale jednotná normalizace v detect_all —
    # čistý detektor kandidáta vrátí, pipeline ho zahodí.
    # 20 minut historie kvůli ATR(14); pokles po 0,5 b, aby close před 10 min
    # zůstal nad entry (podmínka „cena do zdi")
    history = [
        minute(7512 - i * 0.5, flip=7503.5, cum_delta=float(i * 10), idx=i) for i in range(20)
    ]
    history.append(minute(7502, low=7501, flip=7503.5, cum_delta=210.0, idx=20))
    candidate = detect_wall_bounce(history, PARAMS)
    assert candidate is not None
    assert candidate.rrr < PARAMS.min_rrr
    assert SetupTemplate.WALL_BOUNCE not in {c.template for c in detect_all(history, PARAMS)}


# ── R-mechanika (#302) ─────────────────────────────────────────────


def test_average_true_range() -> None:
    # Bary s rozpětím 2 b a nulovou mezerou → ATR = 2; krátká historie = None
    history = [minute(7500, low=7499, high=7501, idx=i) for i in range(15)]
    assert average_true_range(history, 14) == pytest.approx(2.0)
    assert average_true_range(history[:5], 14) is None
    # Nulová volatilita se nedá použít jako měřítko
    assert average_true_range([minute(7500, idx=i) for i in range(15)], 14) is None


def _candidate(entry: float, target: float, stop: float, direction: Direction) -> SetupCandidate:
    return SetupCandidate(
        template=SetupTemplate.WALL_BOUNCE,
        direction=direction,
        entry=entry,
        target=target,
        stop=stop,
        confidence=55,
        reason="test",
    )


def test_normalize_widens_noise_level_stop() -> None:
    """Stop těsnější než min_risk_atr × ATR se rozšíří — risk musí přežít šum."""
    # ATR 10 → minimum 20 b; šablona chtěla stop 4 b (vzor NQ T5 z 27. 7.)
    candidate = _candidate(28650.0, 28450.0, 28654.0, Direction.SHORT)
    normalized = normalize_candidate(candidate, 10.0, PARAMS)
    assert normalized is not None
    assert normalized.risk == pytest.approx(20.0)
    assert normalized.stop == pytest.approx(28670.0)
    assert normalized.context["atr"] == 10.0
    # Zrcadlově long
    long_norm = normalize_candidate(
        _candidate(28650.0, 28850.0, 28646.0, Direction.LONG), 10.0, PARAMS
    )
    assert long_norm is not None
    assert long_norm.stop == pytest.approx(28630.0)


def test_normalize_caps_unreachable_target() -> None:
    """Cíl dál než max_rr × risk se zkrátí na částečný — jinak vždy vyhraje stop."""
    # Vzor NQ failed_break z 27. 7.: cíl 511 b daleko proti stopu 20 b (RRR 25)
    candidate = _candidate(28650.0, 28139.0, 28670.0, Direction.SHORT)
    normalized = normalize_candidate(candidate, 5.0, PARAMS)
    assert normalized is not None
    assert normalized.risk == pytest.approx(20.0)  # ATR floor nezasáhl
    assert normalized.target == pytest.approx(28650.0 - 3.0 * 20.0)
    assert normalized.rrr == pytest.approx(PARAMS.max_rr)


def test_normalize_keeps_reachable_target_and_rejects_low_rrr() -> None:
    # Blízký cíl se nechává být…
    normalized = normalize_candidate(
        _candidate(7500.0, 7530.0, 7490.0, Direction.LONG), 2.0, PARAMS
    )
    assert normalized is not None
    assert normalized.target == 7530.0
    assert normalized.stop == 7490.0
    # …ale pod min_rrr setup nevzniká (dřív kontrolovaly jen T1 a T5)
    assert (
        normalize_candidate(_candidate(7500.0, 7505.0, 7490.0, Direction.LONG), 2.0, PARAMS) is None
    )


def test_normalize_rejects_target_swallowed_by_widened_stop() -> None:
    """Rozšířený stop může cíl přeskočit → záporné RRR, setup nesmí vzniknout."""
    assert (
        normalize_candidate(_candidate(7500.0, 7500.5, 7499.5, Direction.LONG), 50.0, PARAMS)
        is None
    )


def test_detect_all_requires_measurable_atr() -> None:
    """Bez ATR (krátká historie po startu) se risk nedá ověřit → žádné setupy."""
    history = [minute(7512 - i, cum_delta=float(i * 10), idx=i) for i in range(10)]
    history.append(minute(7502, low=7501, cum_delta=110.0, idx=10))
    assert detect_all(history, PARAMS) == []


# ── Kontra-režimový filtr (#252 B) ─────────────────────────────────


def test_is_counter_regime() -> None:
    assert is_counter_regime(Direction.LONG, "negative")
    assert is_counter_regime(Direction.SHORT, "positive")
    assert not is_counter_regime(Direction.LONG, "positive")
    assert not is_counter_regime(Direction.SHORT, "negative")
    # Neznámý režim není kontra — přísnější podmínky se přeskakují
    assert not is_counter_regime(Direction.LONG, None)


def test_wall_bounce_counter_regime_needs_long_flow_confluence() -> None:
    """Long v negativní gammě (#252 B): bez CumΔ konfluence přes 30 min žádný T1."""

    def history_with(warmup_cum: float, count: int = 21) -> list[MinuteInputs]:
        rows = [
            minute(7513, cum_delta=warmup_cum, gex_regime="negative", idx=i) for i in range(count)
        ]
        rows += [
            minute(7512 - i, cum_delta=float(i * 10), gex_regime="negative", idx=count + i)
            for i in range(10)
        ]
        rows.append(minute(7502, low=7501, cum_delta=110.0, gex_regime="negative", idx=count + 10))
        return rows

    # CumΔ před 30 min nad dneškem → tok se na dlouhém okně neotáčí → žádný setup
    assert detect_wall_bounce(history_with(500.0), PARAMS) is None
    # Krátká historie (11 min) — konfluenci nelze ověřit → konzervativně bez setupu
    assert detect_wall_bounce(history_with(0.0, count=0), PARAMS) is None
    # S konfluencí setup vzniká (viz test_detectors_carry_gex_regime_in_context)
    confirmed = detect_wall_bounce(history_with(0.0), PARAMS)
    assert confirmed is not None
    assert confirmed.context["counter_regime"] is True
    # Long v POZITIVNÍ gammě není kontra — projde i bez dlouhé konfluence
    history = [minute(7512 - i, flip=7495.0, cum_delta=float(i * 10), idx=i) for i in range(10)]
    history.append(minute(7502, low=7501, flip=7495.0, cum_delta=110.0, gex_regime="positive"))
    with_trend = detect_wall_bounce(history, PARAMS)
    assert with_trend is not None
    assert with_trend.context["counter_regime"] is False


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


def test_failed_break_counter_regime_needs_flow_confluence() -> None:
    """Reclaim long v negativní gammě (#252 B): jen s CumΔ konfluencí přes 30 min."""
    # Krátká historie (3 min) s kontra-režimem — konfluenci nelze ověřit → None
    short = [
        minute(m.close, low=m.low, cum_delta=m.cum_delta, gex_regime="negative", idx=i)
        for i, m in enumerate(failed_break_history())
    ]
    assert detect_failed_break(short, PARAMS) is None

    def scenario(warmup_cum: float) -> list[MinuteInputs]:
        rows = [minute(7505, cum_delta=warmup_cum, gex_regime="negative", idx=i) for i in range(30)]
        rows.append(minute(7496, low=7473, cum_delta=50.0, gex_regime="negative", idx=30))
        rows.append(minute(7501, low=7495, cum_delta=100.0, gex_regime="negative", idx=31))
        return rows

    # Tok se přes 30 min otáčí nahoru → setup vzniká s příznakem kontra-režimu
    confirmed = detect_failed_break(scenario(0.0), PARAMS)
    assert confirmed is not None
    assert confirmed.direction is Direction.LONG
    assert confirmed.context["counter_regime"] is True
    assert "Kontra-režim potvrzen tokem" in confirmed.reason
    # CumΔ před 30 min výš než teď → bez konfluence → žádný setup
    assert detect_failed_break(scenario(500.0), PARAMS) is None


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
    # #302: stop = pin_stop_ratio × vzdálenost (dřív 1,5× → RRR 0,67)
    assert setup.stop == pytest.approx(7527.5)  # 0.75 × 10 nad entry
    assert setup.rrr == pytest.approx(1 / PARAMS.pin_stop_ratio)


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


# ── T5: divergenční spring (#250, živý vzor 24. 7. 8:49) ──────────


def spring_history(**last_kwargs: object) -> list[MinuteInputs]:
    """90 min chop 7445–7460 s rostoucí CumΔ, pak nové low mimo zónu zdi."""
    rows = [
        minute(
            7450 + (5 if i % 2 else -5),
            low=7445.0,
            high=7460.0,
            put_wall=7400.0,
            call_wall=7485.0,
            flip=None,
            cum_delta=94_000 + i * 100,
            idx=i,
        )
        for i in range(90)
    ]
    defaults: dict[str, object] = dict(
        low=7433.75,
        high=7452.0,
        put_wall=7400.0,
        call_wall=7485.0,
        flip=None,
        cum_delta=103_600.0,
        idx=90,
    )
    defaults.update(last_kwargs)
    rows.append(minute(7436.0, **defaults))  # type: ignore[arg-type]
    return rows


def test_divergence_spring_long_on_new_low_with_cum_max() -> None:
    setup = detect_divergence_spring(spring_history(), PARAMS)
    assert setup is not None
    assert setup.template is SetupTemplate.DIVERGENCE_SPRING
    assert setup.direction is Direction.LONG
    assert setup.entry == 7436.0
    assert setup.stop == pytest.approx(7433.75 - 2.0)  # low − buffer
    assert setup.target == 7485.0  # nejbližší úroveň nad entry (call wall)
    assert setup.context["gex_regime"] is None
    assert "nákupy do slabosti" in setup.reason


def test_detect_all_skips_disabled_templates() -> None:
    """#303: T5 je default vypnutá — čistý detektor ji najde, detect_all zahodí."""
    history = spring_history()
    assert detect_divergence_spring(history, PARAMS) is not None
    assert SetupTemplate.DIVERGENCE_SPRING.value in PARAMS.disabled_templates
    templates = {c.template for c in detect_all(history, PARAMS)}
    assert SetupTemplate.DIVERGENCE_SPRING not in templates

    enabled = replace(PARAMS, disabled_templates=frozenset())
    templates = {c.template for c in detect_all(history, enabled)}
    assert SetupTemplate.DIVERGENCE_SPRING in templates


def test_divergence_spring_requires_divergence_and_distance() -> None:
    # CumΔ NENÍ na maximu okna → žádný spring (jen obyčejný pokles)
    assert detect_divergence_spring(spring_history(cum_delta=95_000.0), PARAMS) is None
    # Nové low v zóně zdi → území T1, T5 mlčí
    assert detect_divergence_spring(spring_history(put_wall=7435.0), PARAMS) is None
    # Bez odmítnutí (close u low) → žádný trigger
    history = spring_history()
    history[-1] = minute(
        7434.0,
        low=7433.75,
        high=7452.0,
        put_wall=7400.0,
        call_wall=7485.0,
        flip=None,
        cum_delta=103_600.0,
        idx=90,
    )
    assert detect_divergence_spring(history, PARAMS) is None


def test_divergence_spring_short_mirror() -> None:
    rows = [
        minute(
            7450 + (5 if i % 2 else -5),
            low=7445.0,
            high=7460.0,
            put_wall=7400.0,
            call_wall=7500.0,
            flip=None,
            max_pain=7440.0,
            cum_delta=-i * 100.0,
            idx=i,
        )
        for i in range(90)
    ]
    rows.append(
        minute(
            7462.0,
            low=7455.0,
            high=7466.0,
            put_wall=7400.0,
            call_wall=7500.0,
            flip=None,
            max_pain=7440.0,
            cum_delta=-20_000.0,
            idx=90,
        )
    )
    setup = detect_divergence_spring(rows, PARAMS)
    assert setup is not None
    assert setup.direction is Direction.SHORT
    assert setup.stop == pytest.approx(7466.0 + 2.0)
    assert setup.target == 7440.0  # Max Pain pod entry


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
        # Dominance zdí (ADR-0010, #223) — SetupEngine je čte z plných levels.
        # Nízké hodnoty potlačují T1: orchestrační testy cílí na T2 (failed_break)
        self.last_gex_levels = GexLevels(
            flip=7515.0,
            call_wall=7530.0,
            put_wall=7500.0,
            centroid=7512.0,
            total_gex=100.0,
            call_wall_dom=0.05,
            put_wall_dom=0.05,
        )


async def test_setup_engine_end_to_end(tmp_path: Path) -> None:
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}")
    repository = SetupsRepository(db)
    repository.ensure_schema()
    oi_repo = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    oi_repo.ensure_schema()
    publisher = RecordingPublisher()
    fake = FakeRuntime()
    runtime = cast(EngineRuntime, fake)
    engine = SetupEngine(
        symbol="ES", repository=repository, oi_repository=oi_repo, publisher=publisher
    )

    def bar(o: float, h: float, low: float, c: float) -> Bar:
        return Bar(ts=TS, open=o, high=h, low=low, close=c, volume=100.0)

    # Kontra-režim (#252 B): reclaim long v negativní gammě (close pod flipem 7515)
    # potřebuje CumΔ konfluenci přes 30 min → warmup s rostoucí CumΔ před scénářem
    for i in range(30):
        fake.last_flow = FakeFlow(float(i))
        await engine.on_minute(
            TS - dt.timedelta(minutes=30 - i), 7505, [bar(7505, 7506, 7504, 7505)], runtime
        )
    fake.last_flow = FakeFlow(100.0)

    # Páteční scénář: baseline → průraz 7500 s dnem 7494 → reclaim 7501 → setup LONG.
    # Dno je mělké schválně (#302): hlubší průraz by dal risk 29 b proti cíli 14 b
    # (RRR 0,48) a normalizace by setup zahodila — přesně ta vada, kterou #302 řeší.
    await engine.on_minute(TS, 7505, [bar(7505, 7506, 7504, 7505)], runtime)
    await engine.on_minute(
        TS + dt.timedelta(minutes=1), 7496, [bar(7500, 7500, 7494, 7496)], runtime
    )
    await engine.on_minute(
        TS + dt.timedelta(minutes=2), 7501, [bar(7496, 7502, 7495, 7501)], runtime
    )

    active = repository.active_for("ES")
    assert len(active) == 1
    assert active[0].template == "failed_break"
    assert active[0].direction == "long"
    assert active[0].stop == 7493  # dno 7494 − 1

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
    assert rows[0]["outcome_r"] == pytest.approx(14 / 8, rel=1e-3)  # cíl 7515, risk 8 b
    assert rows[0]["mfe"] >= 14

    # Ruční hodnocení (jediná mutace po uzavření)
    assert repository.review(rows[0]["id"], 1, "vyšlo přesně podle predikce")
    reviewed = repository.list_for("ES")[0]
    assert reviewed["user_rating"] == 1
    assert reviewed["user_note"] == "vyšlo přesně podle predikce"


async def test_setup_engine_counter_stop_cooldown(tmp_path: Path) -> None:
    """#252 C: stop kontra-setupu → další kontra pokus téže šablony až za 45 min."""
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}")
    repository = SetupsRepository(db)
    repository.ensure_schema()
    oi_repo = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    oi_repo.ensure_schema()
    publisher = RecordingPublisher()
    fake = FakeRuntime()
    runtime = cast(EngineRuntime, fake)
    engine = SetupEngine(
        symbol="ES", repository=repository, oi_repository=oi_repo, publisher=publisher
    )

    async def step(idx: int, o: float, h: float, low: float, c: float, cum: float) -> None:
        fake.last_flow = FakeFlow(cum)
        ts = TS + dt.timedelta(minutes=idx)
        bar = Bar(ts=ts, open=o, high=h, low=low, close=c, volume=100.0)
        await engine.on_minute(ts, c, [bar], runtime)

    async def quiet(idx: int) -> None:
        await step(idx, 7505, 7506, 7504, 7505, float(idx * 10))

    # Warmup 30 min s rostoucí CumΔ (konfluence B nesmí blokovat — testujeme C)
    for i in range(30):
        await quiet(i)
    # Průraz + reclaim → kontra long (close 7501 pod flipem 7515 = negativní gamma).
    # Mělká dna (#302): hlubší průraz by neprošel RRR filtrem normalizace
    await step(30, 7500, 7500, 7494, 7496, 310.0)
    await step(31, 7496, 7502, 7495, 7501, 320.0)
    active = repository.active_for("ES")
    assert len(active) == 1
    assert active[0].template == "failed_break"
    # Stop 7493 zasažen → closed_stop spouští kontra cooldown šablony
    await step(32, 7480, 7481, 7470, 7480, 330.0)
    assert repository.active_for("ES") == []

    # Druhý průraz + reclaim 9 min po stopu → blokováno (45min kontra cooldown),
    # přestože běžný 10min cooldown od vzniku #1 už uplynul
    for i in range(33, 40):
        await quiet(i)
    await step(40, 7500, 7500, 7494, 7496, 400.0)
    await step(41, 7496, 7502, 7495, 7501, 410.0)
    assert repository.active_for("ES") == []

    # Třetí pokus 49 min po stopu → povolen
    for i in range(42, 80):
        await quiet(i)
    await step(80, 7500, 7500, 7494, 7496, 800.0)
    await step(81, 7496, 7502, 7495, 7501, 810.0)
    active = repository.active_for("ES")
    assert len(active) == 1
    assert active[0].template == "failed_break"
    assert active[0].direction == "long"


async def test_setup_engine_close_ts_matches_hitting_bar(tmp_path: Path) -> None:
    """#257: closed_ts patří svíčce, která úroveň zasáhla, ne času cyklu.

    Cyklus nese dávku dvou barů: první (o 7 min starší než `now`) zasáhne cíl,
    druhý by zasáhl stop — uzavření musí být TARGET s ts prvního baru;
    konzervativní stop-first platí jen uvnitř jedné svíčky.
    """
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}")
    repository = SetupsRepository(db)
    repository.ensure_schema()
    oi_repo = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    oi_repo.ensure_schema()
    publisher = RecordingPublisher()
    fake = FakeRuntime()
    runtime = cast(EngineRuntime, fake)
    engine = SetupEngine(
        symbol="ES", repository=repository, oi_repository=oi_repo, publisher=publisher
    )

    async def step(idx: int, o: float, h: float, low: float, c: float, cum: float) -> None:
        fake.last_flow = FakeFlow(cum)
        ts = TS + dt.timedelta(minutes=idx)
        await engine.on_minute(
            ts, c, [Bar(ts=ts, open=o, high=h, low=low, close=c, volume=100.0)], runtime
        )

    for i in range(30):
        await step(i, 7505, 7506, 7504, 7505, float(i * 10))
    await step(30, 7500, 7500, 7494, 7496, 310.0)
    await step(31, 7496, 7502, 7495, 7501, 320.0)  # setup: entry 7501, cíl 7515, stop 7493
    assert len(repository.active_for("ES")) == 1

    # Zpožděný cyklus v now=TS+40 s dávkou barů 33 a 34
    fake.last_flow = FakeFlow(400.0)
    target_bar = Bar(
        ts=TS + dt.timedelta(minutes=33), open=7501, high=7516, low=7500, close=7514, volume=100.0
    )
    stop_bar = Bar(
        ts=TS + dt.timedelta(minutes=34), open=7514, high=7514, low=7470, close=7480, volume=100.0
    )
    await engine.on_minute(TS + dt.timedelta(minutes=40), 7480, [target_bar, stop_bar], runtime)

    rows = repository.list_for("ES")
    assert rows[0]["status"] == "closed_target"
    assert rows[0]["outcome_r"] > 0
    closed_ts = str(rows[0]["closed_ts"])
    assert closed_ts.startswith("2026-07-17T15:33")
    # MFE/MAE se akumulují jen do uzavíracího baru — propad baru 34 (low 7470)
    # se nepočítá; MAE = entry 7501 − low 7500 uzavíracího baru
    assert rows[0]["mae"] == pytest.approx(1.0)
    assert rows[0]["mfe"] == pytest.approx(7516 - 7501)


async def test_setup_engine_blocks_direction_after_stop_streak(tmp_path: Path) -> None:
    """#302: série stopů v jednom směru zablokuje směr napříč šablonami.

    27. 7. vystřelil detektor 20 shortů za sebou proti stoupajícímu NQ —
    per-šablonový anti-spam se dal obejít prokládáním šablon.
    """
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}")
    repository = SetupsRepository(db)
    repository.ensure_schema()
    oi_repo = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    oi_repo.ensure_schema()
    fake = FakeRuntime()
    runtime = cast(EngineRuntime, fake)
    engine = SetupEngine(
        symbol="ES",
        repository=repository,
        oi_repository=oi_repo,
        publisher=RecordingPublisher(),
        # Kontra cooldown vypnutý a T7 vypnutá — testuje se čistě blokace směru;
        # scénář splňuje i podmínky pokračování trendu a druhý setup by test mátl
        params=SetupParams(
            counter_stop_cooldown_minutes=0,
            disabled_templates=frozenset(
                {
                    SetupTemplate.DIVERGENCE_SPRING.value,
                    SetupTemplate.TREND_CONTINUATION.value,
                }
            ),
        ),
    )

    async def step(idx: int, o: float, h: float, low: float, c: float, cum: float) -> None:
        fake.last_flow = FakeFlow(cum)
        ts = TS + dt.timedelta(minutes=idx)
        await engine.on_minute(
            ts, c, [Bar(ts=ts, open=o, high=h, low=low, close=c, volume=100.0)], runtime
        )

    idx = 0
    for _ in range(30):  # warmup (ATR + konfluence CumΔ)
        await step(idx, 7505, 7506, 7504, 7505, float(idx * 10))
        idx += 1

    # Tři cykly průraz → reclaim → stop; každý = jeden long stop
    for attempt in range(3):
        await step(idx, 7500, 7500, 7494, 7496, float(idx * 10))
        idx += 1
        await step(idx, 7496, 7502, 7495, 7501, float(idx * 10))
        idx += 1
        assert len(repository.active_for("ES")) == 1, f"pokus {attempt} nevznikl"
        await step(idx, 7495, 7496, 7490, 7492, float(idx * 10))  # stop 7493
        idx += 1
        assert repository.active_for("ES") == []
        # 16 klidných minut: uplyne cooldown šablony a předchozí stop bar
        # vypadne z reclaim okna (jinak by z něj vzniklo hlubší dno a nižší RRR)
        for _ in range(16):
            await step(idx, 7505, 7506, 7504, 7505, float(idx * 10))
            idx += 1

    stopped = [r for r in repository.list_for("ES") if r["status"] == "closed_stop"]
    assert len(stopped) == 3

    # Čtvrtý pokus se stejným vzorem → směr long je zablokovaný
    await step(idx, 7500, 7500, 7494, 7496, float(idx * 10))
    idx += 1
    await step(idx, 7496, 7502, 7495, 7501, float(idx * 10))
    idx += 1
    assert repository.active_for("ES") == []

    # Po uplynutí blokace (90 min) tentýž vzor projde
    idx += 95
    await step(idx, 7500, 7500, 7494, 7496, float(idx * 10))
    idx += 1
    await step(idx, 7496, 7502, 7495, 7501, float(idx * 10))
    assert len(repository.active_for("ES")) == 1


async def test_setup_engine_stale_setup_times_out_by_own_expiry(tmp_path: Path) -> None:
    """#259: setup proslé expirace se po restartu uzavře timeoutem svojí expirace.

    Runtime jede na čerstvé expiraci (timeout z ní by setup nechal žít) a dnešní
    bar zasahuje jeho target — bary po settle se ale nevyhodnocují, takže výhra
    se falešně nepřipíše.
    """
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}")
    repository = SetupsRepository(db)
    repository.ensure_schema()
    oi_repo = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    oi_repo.ensure_schema()
    # Včerejší setup (expirace 20260716, settle 16. 7. 20:00 UTC < TS)
    repository.create(
        symbol="ES",
        expiry="20260716",
        template="wall_bounce",
        direction="long",
        created_ts=TS - dt.timedelta(days=1),
        entry=7452.0,
        target=7540.0,
        stop=7428.0,
        confidence=55,
        reason="test",
        context={},
    )
    # Restart enginu: __post_init__ načte aktivní setup z DB
    engine = SetupEngine(
        symbol="ES",
        repository=repository,
        oi_repository=oi_repo,
        publisher=RecordingPublisher(),
    )
    runtime = cast(EngineRuntime, FakeRuntime())
    bar = Bar(ts=TS, open=7538.0, high=7545.0, low=7538.0, close=7460.0, volume=100.0)
    await engine.on_minute(TS, 7460, [bar], runtime)

    rows = repository.list_for("ES")
    assert rows[0]["status"] == "closed_timeout"  # ne closed_target z dnešního high
    assert rows[0]["outcome_r"] == pytest.approx((7460 - 7452) / 24, rel=1e-3)


# ── Verzování mechaniky (#311) ─────────────────────────────────────


def test_new_setups_carry_current_mechanics_version(tmp_path: Path) -> None:
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}")
    repository = SetupsRepository(db)
    repository.ensure_schema()
    setup_id = repository.create(
        symbol="ES",
        expiry="20260717",
        template="wall_bounce",
        direction="long",
        created_ts=TS,
        entry=7500.0,
        target=7530.0,
        stop=7490.0,
        confidence=55,
        reason="test",
        context={},
    )
    row = next(r for r in repository.list_for("ES") if r["id"] == setup_id)
    assert row["mechanics_version"] == SETUP_MECHANICS_VERSION


def test_legacy_rows_get_version_1_and_are_excluded(tmp_path: Path) -> None:
    """Migrace doplní sloupec do existující tabulky; staré řádky = v1 (#311).

    Hranice tím vyjde správně: všechno před migrací vzniklo starou mechanikou
    nebo nad zmrzlými daty (ADR-0015), takže do bilance aktuálního systému
    nepatří.
    """
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'legacy.sqlite'}")
    # Tabulka „z doby před #311" — bez sloupce mechanics_version
    with db.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE setups (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "symbol VARCHAR(16) NOT NULL, expiry VARCHAR(8) NOT NULL, "
                "template VARCHAR(32) NOT NULL, direction VARCHAR(8) NOT NULL, "
                "created_ts DATETIME NOT NULL, entry FLOAT NOT NULL, target FLOAT NOT NULL, "
                "stop FLOAT NOT NULL, confidence INTEGER NOT NULL, reason TEXT NOT NULL, "
                "context JSON NOT NULL, status VARCHAR(16) NOT NULL, closed_ts DATETIME, "
                "outcome_r FLOAT, mfe FLOAT, mae FLOAT, user_rating INTEGER, user_note TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO setups (symbol, expiry, template, direction, created_ts, entry, "
                "target, stop, confidence, reason, context, status, closed_ts, outcome_r) "
                "VALUES ('ES','20260717','failed_break','short', :ts, 7500, 7200, 7505, 55, "
                "'stara mechanika', '{}', 'closed_stop', :ts, -1.0)"
            ),
            {"ts": TS},
        )

    repository = SetupsRepository(db)
    repository.ensure_schema()  # idempotentní ALTER
    repository.ensure_schema()  # podruhé nesmí spadnout

    since = TS - dt.timedelta(days=7)
    assert len(repository.closed_since("ES", since)) == 1  # bez filtru se vidí
    assert repository.closed_since("ES", since, mechanics_version=SETUP_MECHANICS_VERSION) == []

    # Nový setup už nese aktuální verzi a do bilance patří
    new_id = repository.create(
        symbol="ES",
        expiry="20260717",
        template="wall_bounce",
        direction="long",
        created_ts=TS,
        entry=7500.0,
        target=7530.0,
        stop=7490.0,
        confidence=55,
        reason="nova mechanika",
        context={},
    )
    repository.close(new_id, status="closed_target", closed_ts=TS, outcome_r=2.0, mfe=0.0, mae=0.0)
    current = repository.closed_since("ES", since, mechanics_version=SETUP_MECHANICS_VERSION)
    assert len(current) == 1
    assert current[0].outcome_r == 2.0


# ── ATR škálování prahů (#434) ────────────────────────────────────────


def test_scale_params_is_identity_with_default_multipliers() -> None:
    """Výchozí násobky 0 = chování beze změny (škálování zamítnuto měřením)."""
    params = SetupParams()
    scaled = scale_params(params, atr=11.5)
    assert scaled.wall_zone == params.wall_zone
    assert scaled.rejection_min == params.rejection_min


def test_scale_params_keeps_absolute_value_as_floor() -> None:
    """Na klidném trhu (malé ATR) drží absolutní práh, jinak rozhoduje ATR."""
    params = SetupParams(wall_zone=3.0, wall_zone_atr=1.9, rejection_min=1.0, rejection_min_atr=0.6)
    calm = scale_params(params, atr=0.5)
    assert calm.wall_zone == 3.0  # 1,9 × 0,5 = 0,95 < 3,0 → spodní mez
    volatile = scale_params(params, atr=11.5)
    assert volatile.wall_zone == pytest.approx(21.85)
    assert volatile.rejection_min == pytest.approx(6.9)


# ── T7 pokračování trendu (#443) ──────────────────────────────────────


def trend_minute(
    idx: int,
    close: float,
    low: float,
    high: float,
    *,
    flip: float,
    put_wall: float,
    call_wall: float,
) -> MinuteInputs:
    return MinuteInputs(
        ts=TS + dt.timedelta(minutes=idx),
        open=close,
        high=high,
        low=low,
        close=close,
        flip=flip,
        call_wall=call_wall,
        put_wall=put_wall,
        max_pain=None,
        cum_delta=0.0,
        call_flow=0.0,
        put_flow=0.0,
        opt_vol=0.0,
        minutes_to_expiry=300.0,
    )


def test_trend_continuation_fires_on_pullback_above_support_wall() -> None:
    """Cena utekla put zdi, pullback k EMA a odmítnutí → long po trendu."""
    params = SetupParams()
    levels = {"flip": 7400.0, "put_wall": 7420.0, "call_wall": 7600.0}
    # Stoupající trend, ať EMA leží pod cenou
    history = [trend_minute(i, 7480.0 + i, 7479.0 + i, 7482.0 + i, **levels) for i in range(40)]
    atr = average_true_range(history, params.atr_lookback) or 1.0
    ema = _ema([m.close for m in history], params.trend_ema_span)
    assert ema is not None
    # Poslední minuta: knot dolů k EMA, close zpět nad ní
    history.append(trend_minute(40, ema + 0.5 * atr, ema - 0.1 * atr, ema + 0.6 * atr, **levels))

    candidate = detect_trend_continuation(history, params, atr)

    assert candidate is not None
    assert candidate.direction is Direction.LONG
    assert candidate.target == levels["call_wall"]  # cíl je protilehlá zeď
    assert candidate.stop < candidate.entry


def test_trend_continuation_skips_price_close_to_support_wall() -> None:
    """U zdi patří scéna T1 odrazu, ne trendové šabloně."""
    params = SetupParams()
    levels = {"flip": 7400.0, "put_wall": 7478.0, "call_wall": 7600.0}
    history = [trend_minute(i, 7480.0 + i, 7479.0 + i, 7482.0 + i, **levels) for i in range(41)]
    atr = average_true_range(history, params.atr_lookback) or 1.0

    assert detect_trend_continuation(history, params, atr) is None


def test_trend_continuation_needs_positive_gamma_side() -> None:
    """Long pod flipem = jiný režim; šablona je pro pokračování v gammě."""
    params = SetupParams()
    levels = {"flip": 7900.0, "put_wall": 7420.0, "call_wall": 7600.0}
    history = [trend_minute(i, 7480.0 + i, 7479.0 + i, 7482.0 + i, **levels) for i in range(41)]
    atr = average_true_range(history, params.atr_lookback) or 1.0

    assert detect_trend_continuation(history, params, atr) is None


def test_gamma_momentum_cum_gate_uses_quantile_not_absolute_extreme() -> None:
    """CumΔ musí směr POTVRZOVAT, ne padnout rekord (#443).

    Původní podmínka „CumΔ = absolutní minimum okna" byla tak přísná, že
    z 11 křížení flipu neprošlo ani jedno (0 setupů z 280 za celou historii).
    """
    # Klesající CumΔ: poslední hodnota je nízko, ale NENÍ absolutní minimum
    history = [
        minute(
            7510.0,
            flip=7500.0,
            put_wall=7400.0,
            call_wall=7600.0,
            cum_delta=100.0 - i,
            put_flow=10.0,
            idx=i,
        )
        for i in range(30)
    ]
    history[5] = replace(history[5], cum_delta=-999.0)  # rekordní minimum dřív v okně
    # Průraz flipu dolů a držení pod ním
    history.append(
        minute(
            7490.0,
            flip=7500.0,
            put_wall=7400.0,
            call_wall=7600.0,
            cum_delta=60.0,
            put_flow=10.0,
            idx=30,
        )
    )

    strict = detect_gamma_momentum(history, SetupParams(momentum_cum_quantile=0.0))
    relaxed = detect_gamma_momentum(history, SetupParams(momentum_cum_quantile=0.25))

    assert strict is None  # rekord padl dřív → původní brána neprojde
    assert relaxed is not None
    assert relaxed.direction is Direction.SHORT


def test_settle_ts_expirace_v_burzovni_zone() -> None:
    """#511: settle expirace 16:00 ET — letní 20:00 UTC (dřívější chování), zimní 21:00."""
    assert SetupEngine._settle_ts("20260717") == dt.datetime(2026, 7, 17, 20, 0, tzinfo=dt.UTC)
    assert SetupEngine._settle_ts("20260115") == dt.datetime(2026, 1, 15, 21, 0, tzinfo=dt.UTC)
    assert SetupEngine._settle_ts("nesmysl") is None
    # Timeout setupu se v zimě posouvá se settle: ve 20:30 UTC ještě 30 minut zbývá
    now = dt.datetime(2026, 1, 15, 20, 30, tzinfo=dt.UTC)
    assert SetupEngine._minutes_to_expiry("20260115", now) == 30.0
