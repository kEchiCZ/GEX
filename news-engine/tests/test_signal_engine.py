"""Golden testy Signal enginu (#294, SPEC kap. 6 + kap. 10: Wilson gate)."""

import datetime as dt

import pytest

from gexlens_news.signal_engine import (
    BucketStats,
    GexContext,
    SignalEvent,
    evaluate_event,
    event_is_fresh,
    gate_passes,
)

NOW = dt.datetime(2026, 7, 29, 14, 0, tzinfo=dt.UTC)


def stats(n: int = 50, hit_rate_lb: float | None = 0.58, ret_mean_bp: float = 6.0) -> BucketStats:
    return BucketStats(n=n, hit_rate_lb=hit_rate_lb, ret_mean_bp=ret_mean_bp, window_min=5)


def event(
    score: float = 0.6,
    *,
    category: str = "FED",
    importance: int = 3,
    age_min: float = 10.0,
) -> SignalEvent:
    return SignalEvent(
        event_id=1,
        ts_event=NOW - dt.timedelta(minutes=age_min),
        category=category,
        importance=importance,
        score=score,
        surprise_bucket="none",
        deferred=False,
        classification_version=2,
    )


# ── Wilson gate (SPEC 6.2, golden dle kap. 10) ─────────────────────


def test_gate_requires_samples_and_wilson_lb() -> None:
    assert gate_passes(stats(n=30, hit_rate_lb=0.51))
    assert not gate_passes(stats(n=29, hit_rate_lb=0.9))  # málo vzorků
    assert not gate_passes(stats(n=200, hit_rate_lb=0.50))  # LB přesně 0.50 nestačí
    assert not gate_passes(stats(hit_rate_lb=None))  # bucket bez hit-rate
    assert not gate_passes(None)  # bucket vůbec neexistuje


# ── Čerstvost a expirace (ADR-0020) ────────────────────────────────


def test_freshness_is_bounded_by_half_life() -> None:
    # FED importance 3: τ = 180 × 1.5 = 270 min
    assert event_is_fresh(event(age_min=269), NOW)
    assert not event_is_fresh(event(age_min=271), NOW)
    # Budoucí (plánovaný) event signál nezakládá
    assert not event_is_fresh(event(age_min=-5), NOW)


def test_expiry_is_event_ts_plus_half_life() -> None:
    signal_event = event(age_min=10)  # τ = 270 min
    candidates = evaluate_event(signal_event, state="RiskOn", stats=stats(), now=NOW, gex=None)
    assert candidates
    assert candidates[0].expiry_ts == signal_event.ts_event + dt.timedelta(minutes=270)


# ── Pravidlová logika (SPEC 6.3) ───────────────────────────────────


def test_long_needs_riskon_positive_score_and_positive_bucket() -> None:
    ok = evaluate_event(event(0.6), state="RiskOn", stats=stats(), now=NOW, gex=None)
    assert len(ok) == 1  # NEWS větev (COMBINED bez kontextu nevzniká)
    assert ok[0].direction == "long"
    assert ok[0].mode == "NEWS"
    assert ok[0].strength == pytest.approx(0.6)

    # Neutral stav → nic
    assert evaluate_event(event(0.6), state="Neutral", stats=stats(), now=NOW, gex=None) == []
    # Záporné skóre v RiskOn → nic (nesouhlasí směr)
    assert evaluate_event(event(-0.6), state="RiskOn", stats=stats(), now=NOW, gex=None) == []
    # Bucket s negativní očekávanou reakcí → long nevznikne
    assert (
        evaluate_event(event(0.6), state="RiskOn", stats=stats(ret_mean_bp=-3.0), now=NOW, gex=None)
        == []
    )


def test_short_is_mirror() -> None:
    result = evaluate_event(
        event(-0.8), state="RiskOff", stats=stats(ret_mean_bp=-5.0), now=NOW, gex=None
    )
    assert len(result) == 1
    assert result[0].direction == "short"
    assert result[0].strength == pytest.approx(0.8)


def test_strength_is_clamped_to_one() -> None:
    result = evaluate_event(event(1.7), state="RiskOn", stats=stats(), now=NOW, gex=None)
    assert result[0].strength == 1.0


def test_inputs_snapshot_is_complete() -> None:
    """SPEC 6.3: signál musí být zpětně vysvětlitelný — event, verze, bucket."""
    result = evaluate_event(event(0.6), state="RiskOn", stats=stats(), now=NOW, gex=None)
    inputs = result[0].inputs
    assert inputs["event_id"] == 1
    assert inputs["classification_version"] == 2
    assert inputs["bucket"] == {"n": 50, "hit_rate_lb": 0.58, "ret_mean_bp": 6.0, "window_min": 5}
    assert inputs["state"] == "RiskOn"
    assert inputs["half_life_min"] == 270


# ── COMBINED větev (SPEC 6.1) ──────────────────────────────────────


def test_combined_requires_supporting_gex_context() -> None:
    # Spot nad flipem → long kontext souhlasí → NEWS i COMBINED
    supportive = GexContext(spot=7500.0, flip=7450.0, cum_delta_slope=None)
    both = evaluate_event(event(0.6), state="RiskOn", stats=stats(), now=NOW, gex=supportive)
    assert [c.mode for c in both] == ["NEWS", "COMBINED"]
    assert both[1].inputs["gex"] == {"spot": 7500.0, "flip": 7450.0, "cum_delta_slope": None}

    # Spot pod flipem a klesající CumΔ → long kontext nesouhlasí → jen NEWS
    opposing = GexContext(spot=7400.0, flip=7450.0, cum_delta_slope=-500.0)
    only_news = evaluate_event(event(0.6), state="RiskOn", stats=stats(), now=NOW, gex=opposing)
    assert [c.mode for c in only_news] == ["NEWS"]

    # Rostoucí CumΔ podporuje long i pod flipem (SPEC 6.3: „NEBO CumΔ rostoucí")
    rising = GexContext(spot=7400.0, flip=7450.0, cum_delta_slope=800.0)
    with_rising = evaluate_event(event(0.6), state="RiskOn", stats=stats(), now=NOW, gex=rising)
    assert [c.mode for c in with_rising] == ["NEWS", "COMBINED"]


def test_combined_short_mirror_context() -> None:
    below_flip = GexContext(spot=7400.0, flip=7450.0, cum_delta_slope=None)
    result = evaluate_event(
        event(-0.7), state="RiskOff", stats=stats(ret_mean_bp=-4.0), now=NOW, gex=below_flip
    )
    assert [c.mode for c in result] == ["NEWS", "COMBINED"]
    assert all(c.direction == "short" for c in result)
