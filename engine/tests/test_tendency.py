"""Testy indikátoru tendence (#350): hlasy složek, strop, pásma, storage."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.gexfield import GexProfile, gamma_at_price
from gexlens_engine.compute.tendency import (
    COMPONENT_CAP,
    TENDENCY_WEIGHTS_VERSION,
    TendencyInputs,
    TendencyResult,
    band_of,
    evaluate_tendency,
)
from gexlens_engine.storage.tendency_store import TendencyRepository

NOW = dt.datetime(2026, 7, 30, 14, 0, tzinfo=dt.UTC)


def inputs(**overrides: object) -> TendencyInputs:
    values: dict[str, object] = {"ts_min": NOW, "spot": 7450.0}
    values.update(overrides)
    return TendencyInputs(**values)  # type: ignore[arg-type]


def vote_of(result: TendencyResult, name: str) -> float:
    votes = {vote.name: vote.vote for vote in result.votes}
    return votes[name]


def test_band_thresholds_match_issue() -> None:
    """Prahy z issue #350: ±0,15 a ±0,5; Neutral schválně široký."""
    assert band_of(-0.6) == "strong_short"
    assert band_of(-0.5) == "strong_short"
    assert band_of(-0.3) == "short"
    assert band_of(-0.15) == "short"
    assert band_of(0.0) == "neutral"
    assert band_of(0.149) == "neutral"
    assert band_of(0.15) == "long"
    assert band_of(0.49) == "long"
    assert band_of(0.5) == "strong_long"


def test_component_votes_follow_legend_rules() -> None:
    result = evaluate_tendency(
        inputs(
            flip=7440.0,  # cena nad flipem → long
            call_wall=7500.0,
            put_wall=7400.0,  # cena uprostřed → 0
            call_wall_dom=0.1,
            put_wall_dom=0.3,  # silnější put zeď → long
            max_pain=7460.0,  # cena pod Max Pain → long
            centroid=7445.0,  # cena nad těžištěm → short
            cum_delta_now=500.0,
            cum_delta_then=300.0,  # roste → long
            price_then=7460.0,  # cena klesla a CumΔ roste → rozchod long
            call_flow=300.0,
            put_flow=100.0,  # převaha call → long (0,5)
            sent_value=0.4,
            sent_value_then=0.2,  # kladný a roste → long
            gamma_at_price=-120.0,  # záporná gamma → short
        )
    )
    assert result is not None
    assert vote_of(result, "flip") == 1.0
    assert vote_of(result, "walls_distance") == 0.0
    assert vote_of(result, "walls_dominance") == pytest.approx((0.3 - 0.1) / 0.3)
    assert vote_of(result, "max_pain") == 1.0
    assert vote_of(result, "centroid") == -1.0
    assert vote_of(result, "cum_delta_slope") == 1.0
    assert vote_of(result, "divergence") == 1.0
    assert vote_of(result, "delta_flow") == pytest.approx(0.5)
    assert vote_of(result, "sentindex") == 1.0
    assert vote_of(result, "gamma_at_price") == -1.0
    assert result.weights_version == TENDENCY_WEIGHTS_VERSION
    assert 0 < result.score <= 1


def test_missing_components_are_skipped_not_zeroed() -> None:
    result = evaluate_tendency(inputs(flip=7400.0))
    assert result is not None
    assert [vote.name for vote in result.votes] == ["flip"]
    # Jediná složka nesmí sama vytlačit skóre do Strong pásma (strop #350)
    assert result.score == pytest.approx(COMPONENT_CAP)
    assert result.band == "long"
    assert evaluate_tendency(inputs()) is None  # žádná data → žádný výsledek


def test_walls_distance_is_continuous() -> None:
    near_put = evaluate_tendency(inputs(call_wall=7500.0, put_wall=7400.0, spot=7410.0))
    near_call = evaluate_tendency(inputs(call_wall=7500.0, put_wall=7400.0, spot=7490.0))
    assert near_put is not None and near_call is not None
    assert vote_of(near_put, "walls_distance") == pytest.approx(0.8)
    assert vote_of(near_call, "walls_distance") == pytest.approx(-0.8)


def test_gamma_at_price_interpolates_grid() -> None:
    profile = GexProfile(ts_min=NOW, grid_start=7400.0, grid_step=10.0, values=(0.0, 100.0, -50.0))
    assert gamma_at_price(profile, 7405.0) == pytest.approx(50.0)
    assert gamma_at_price(profile, 7390.0) == 0.0  # pod mřížkou → krajní hodnota
    assert gamma_at_price(profile, 7500.0) == -50.0
    assert gamma_at_price(GexProfile(ts_min=NOW, grid_start=0, grid_step=10, values=()), 1) is None


def test_repository_upserts_votes_and_version(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'tendency.sqlite'}")
    repository = TendencyRepository(engine)
    repository.ensure_schema()
    repository.ensure_schema()  # idempotentní

    result = evaluate_tendency(inputs(flip=7400.0, max_pain=7460.0))
    assert result is not None
    repository.upsert("ES", result)
    repository.upsert("ES", result)  # táž minuta → přepis, ne duplikát

    rows = repository.series_for("ES", NOW.date())
    assert len(rows) == 1
    assert rows[0]["band"] == result.band
    assert rows[0]["weights_version"] == TENDENCY_WEIGHTS_VERSION
    assert {vote["name"] for vote in rows[0]["votes"]} == {"flip", "max_pain"}
    assert repository.series_for("ES", NOW.date() + dt.timedelta(days=1)) == []


def test_charm_flow_votes_against_net_charm_with_time_ramp() -> None:
    """#397: dealer tok = −sign(charm); rampa 0 → 1 mezi T−4 h a T−1 h."""
    from gexlens_engine.compute.tendency import charm_time_factor

    assert charm_time_factor(30.0) == 1.0
    assert charm_time_factor(150.0) == pytest.approx(0.5)
    assert charm_time_factor(300.0) == 0.0
    assert charm_time_factor(-15.0) == 0.0  # po close se nehlasuje

    # Záporný charm (put masa pod cenou) hodinu před close → plný long hlas
    late = evaluate_tendency(inputs(charm_at_price=-500.0, minutes_to_close=45.0))
    assert late is not None
    assert vote_of(late, "charm_flow") == 1.0
    # Kladný charm v půlce rampy → poloviční short hlas
    mid = evaluate_tendency(inputs(charm_at_price=800.0, minutes_to_close=150.0))
    assert mid is not None
    assert vote_of(mid, "charm_flow") == pytest.approx(-0.5)
    # Ráno (za rampou) hlas 0 — složka je vidět, ale mlčí
    morning = evaluate_tendency(inputs(charm_at_price=-500.0, minutes_to_close=400.0))
    assert morning is not None
    assert vote_of(morning, "charm_flow") == 0.0
    # Bez minutes_to_close se složka přeskakuje
    missing = evaluate_tendency(inputs(charm_at_price=-500.0))
    assert missing is None or "charm_flow" not in {v.name for v in missing.votes}


def test_vanna_flow_needs_iv_trend() -> None:
    """#397: hlas = sign(vanna) × směr IV; plochá IV (deadband) mlčí."""
    base = dict(vanna_at_price=200.0, iv_now=0.148, iv_then=0.150)  # IV klesá o 0,2 b
    falling = evaluate_tendency(inputs(**base))
    assert falling is not None
    assert vote_of(falling, "vanna_flow") == 1.0  # kladná vanna + pokles IV → nákupy
    rising = evaluate_tendency(inputs(vanna_at_price=200.0, iv_now=0.152, iv_then=0.150))
    assert rising is not None
    assert vote_of(rising, "vanna_flow") == -1.0
    negative = evaluate_tendency(inputs(vanna_at_price=-200.0, iv_now=0.148, iv_then=0.150))
    assert negative is not None
    assert vote_of(negative, "vanna_flow") == -1.0
    flat = evaluate_tendency(inputs(vanna_at_price=200.0, iv_now=0.1505, iv_then=0.150))
    assert flat is not None
    assert vote_of(flat, "vanna_flow") == 0.0  # pod deadbandem
    # Bez IV řady se složka přeskakuje
    missing = evaluate_tendency(inputs(vanna_at_price=200.0, iv_now=0.15))
    assert missing is None or "vanna_flow" not in {v.name for v in missing.votes}


def test_weights_version_bumped_for_v2() -> None:
    assert TENDENCY_WEIGHTS_VERSION == 2
