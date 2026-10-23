"""Testy indikátoru tendence (#350): hlasy složek, strop, pásma, storage."""

import datetime as dt
import logging
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine
from test_setups import VOLUME_PLAN, QuotesRuntime, quote

from gexlens_engine.compute.cumdelta import FlowRow
from gexlens_engine.compute.gexfield import GexProfile, gamma_at_price
from gexlens_engine.compute.setups import CUMDELTA_PRINTS
from gexlens_engine.compute.tendency import (
    COMPONENT_CAP,
    TENDENCY_RULES_V4,
    TENDENCY_RULES_V5,
    TENDENCY_WEIGHTS_VERSION,
    WEIGHTS,
    WEIGHTS_V5,
    BandHysteresis,
    TendencyInputs,
    TendencyResult,
    _collect_votes,
    band_of,
    evaluate_tendency,
)
from gexlens_engine.runtime import EngineRuntime
from gexlens_engine.storage.tendency_store import TendencyRepository
from gexlens_engine.tendency import TendencyEngine

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
            cum_delta_slope=200.0,  # roste → long
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


def test_weights_version_follows_mechanics() -> None:
    """v4 (#1241) pod mechanikou v5; v5 (#1460) zapíná až bump mechaniky v6 (#1403)."""
    assert TENDENCY_RULES_V4 == 4 and TENDENCY_RULES_V5 == 5
    live = TENDENCY_RULES_V5 if CUMDELTA_PRINTS else TENDENCY_RULES_V4
    assert live == TENDENCY_WEIGHTS_VERSION


def test_trendovy_den_poloha_a_max_pain_nehlasuji_proti_pohybu() -> None:
    """#1241 (21. 9. 2026): slabá/posouvající se zeď vypne hlas polohy, Max Pain jen u close."""

    def inputs(**overrides: float | None) -> TendencyInputs:
        values: dict[str, float | None] = {
            "flip": 29840.0,
            "put_wall": 29875.0,
            "max_pain": 29750.0,
            "call_wall": 30500.0,
            "put_wall_dom": 0.2,
        }
        values.update(overrides)
        return TendencyInputs(
            ts_min=NOW,
            spot=30500.0,
            flip=values.get("flip"),
            call_wall=values.get("call_wall"),
            put_wall=values.get("put_wall"),
            call_wall_dom=values.get("call_wall_dom"),
            put_wall_dom=values.get("put_wall_dom"),
            max_pain=values.get("max_pain"),
            call_wall_then=values.get("call_wall_then"),
            minutes_to_close=values.get("minutes_to_close"),
        )

    # Call zeď 30 500 s dominancí 0,18 → poloha nehlasuje (0), Max Pain 3 h do close nehlasuje
    votes = {v.name: v for v in _collect_votes(inputs(call_wall_dom=0.18, minutes_to_close=200.0))}
    assert votes["walls_distance"].vote == 0.0 and "slabá" in votes["walls_distance"].detail
    assert "max_pain" not in votes
    # Silná zeď, ale za 30 min se posunula za cenou (30 300 → 30 650) → poloha nehlasuje
    votes = {
        v.name: v
        for v in _collect_votes(
            inputs(
                call_wall=30650.0, call_wall_then=30300.0, call_wall_dom=0.4, minutes_to_close=60.0
            )
        )
    }
    assert votes["walls_distance"].vote == 0.0 and "posouvá" in votes["walls_distance"].detail
    assert votes["max_pain"].vote == -1.0  # 60 min do close: Max Pain pod cenou hlasuje short
    # Silná stojící zeď: poloha hlasuje jako dřív (blíž k call zdi = short)
    votes = {
        v.name: v
        for v in _collect_votes(
            inputs(call_wall=30650.0, call_wall_then=30650.0, call_wall_dom=0.4)
        )
    }
    assert votes["walls_distance"].vote < 0


# ── Hystereze pásem (#394) ────────────────────────────────────────────


def test_hysteresis_ignores_flicker_around_threshold() -> None:
    """Kmit těsně kolem prahu ±0,15 pásmo nepřepne (margin 0,05)."""
    hyst = BandHysteresis()
    assert hyst.update(0.10) == "neutral"  # první minuta usadí stav
    # 0,16–0,19 je za prahem long, ale ne o margin → drží neutral
    for score in (0.16, 0.19, 0.14, 0.18):
        assert hyst.update(score) == "neutral"


def test_hysteresis_switches_after_margin_and_dwell() -> None:
    """Skóre za prahem o margin musí vydržet dwell minut, pak se přepne."""
    hyst = BandHysteresis()
    assert hyst.update(0.0) == "neutral"
    assert hyst.update(0.25) == "neutral"  # 1. minuta za 0,15 + 0,05
    assert hyst.update(0.30) == "neutral"  # 2. minuta
    assert hyst.update(0.28) == "long"  # 3. minuta (dwell 3) → přepnutí
    # Návrat pod práh se řídí stejnými pravidly z pásma long
    assert hyst.update(0.05) == "long"  # 1. minuta pod 0,15 − 0,05
    assert hyst.update(0.16) == "long"  # návrat do long maže rozpracované přepnutí
    assert hyst.update(0.05) == "long"
    assert hyst.update(0.02) == "long"
    assert hyst.update(0.08) == "neutral"


def test_hysteresis_pending_resets_on_return_to_band() -> None:
    """Přerušený pokus o přepnutí se nesčítá přes návraty do pásma."""
    hyst = BandHysteresis()
    hyst.update(0.0)
    hyst.update(0.25)
    hyst.update(0.25)
    hyst.update(0.10)  # návrat → počítadlo na nule
    assert hyst.update(0.25) == "neutral"
    assert hyst.update(0.25) == "neutral"
    assert hyst.update(0.25) == "long"


def test_evaluate_tendency_uses_hysteresis_when_given() -> None:
    """Skóre se hysterezí nemění, filtruje se jen pásmo."""
    hyst = BandHysteresis()
    first = evaluate_tendency(inputs(centroid=7449.0), hysteresis=hyst)  # slabý short hlas
    assert first is not None
    assert first.band == band_of(first.score)  # první minuta = bez filtru
    strong = evaluate_tendency(inputs(flip=7400.0), hysteresis=hyst)  # skok do long
    assert strong is not None
    assert strong.band == first.band  # dwell 3 min ještě drží původní pásmo
    assert strong.score == pytest.approx(COMPONENT_CAP)  # skóre samotné bez filtru


def test_minutes_to_close_odvozene_ze_settle_burzovni_zony() -> None:
    """#511: rampa charm hlasu míří na settle 16:00 ET — 20:00 UTC v létě, 21:00 v zimě."""
    from gexlens_engine.tendency import TendencyEngine

    summer = dt.datetime(2026, 7, 30, 19, 0, tzinfo=dt.UTC)
    assert TendencyEngine._minutes_to_close(summer) == 60.0
    winter = dt.datetime(2026, 1, 15, 20, 0, tzinfo=dt.UTC)
    assert TendencyEngine._minutes_to_close(winter) == 60.0  # fixních 20:00 UTC by dalo 0
    po_close = dt.datetime(2026, 1, 15, 21, 30, tzinfo=dt.UTC)
    assert TendencyEngine._minutes_to_close(po_close) == -30.0


def test_sentiment_reads_per_symbol_partition_and_warns_when_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#1136: partice leží v derived/sentiment/{SYMBOL}/{den}.parquet (ADR-0026),
    plochá cesta bez symbolu se nečte; chybějící partice = warning (škrcený), ne ticho."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from gexlens_engine.tendency import SENTIMENT_SUBDIR, TendencyEngine

    now = dt.datetime(2026, 9, 11, 15, 0, tzinfo=dt.UTC)
    day = now.date().isoformat()
    # Plochá (stará) cesta existuje, ale je to jiná hodnota — nesmí se použít
    flat = tmp_path / "derived" / SENTIMENT_SUBDIR
    flat.mkdir(parents=True)
    pq.write_table(pa.table({"ts_min": [now], "value": [9.9]}), flat / f"{day}.parquet")
    per_symbol = flat / "ES"
    per_symbol.mkdir()
    pq.write_table(
        pa.table(
            {
                "ts_min": [now - dt.timedelta(minutes=45), now - dt.timedelta(minutes=1)],
                "value": [0.10, 0.25],
            }
        ),
        per_symbol / f"{day}.parquet",
    )
    engine = TendencyEngine.__new__(TendencyEngine)
    engine.symbol = "ES"
    engine.data_dir = tmp_path
    engine._sent_cache = {}
    engine._sent_missing_warned_at = None
    current, then = engine._sentiment(now)
    assert current == pytest.approx(0.25)
    assert then == pytest.approx(0.10)

    # NQ partici nemá → (None, None) + jeden warning, druhé volání v okně mlčí
    engine.symbol = "NQ"
    engine._sent_cache = {}
    with caplog.at_level(logging.WARNING, logger="gexlens_engine.tendency"):
        assert engine._sentiment(now) == (None, None)
        assert engine._sentiment(now + dt.timedelta(minutes=5)) == (None, None)
    warnings = [r for r in caplog.records if "SentIndex partice" in r.getMessage()]
    assert len(warnings) == 1
    assert "NQ" in warnings[0].getMessage()


# ── Tendence v5 (#1460, mechanika v6) ─────────────────────────────────


def test_v5_poloha_mezi_zdmi_hlasuje_s_vahou_nula() -> None:
    """E-0.12 C (#1113): hlas se ukládá s vahou 0 a do skóre nevstupuje."""
    assert WEIGHTS_V5["walls_distance"] == 0.0
    assert {k: v for k, v in WEIGHTS_V5.items() if k != "walls_distance"} == {
        k: v for k, v in WEIGHTS.items() if k != "walls_distance"
    }
    base = dict(flip=7440.0, centroid=7445.0)  # long 3 a short 1
    walls = dict(call_wall=7500.0, put_wall=7400.0, spot=7410.0)  # u put zdi → +0,8
    v5 = evaluate_tendency(inputs(**base, **walls), version=TENDENCY_RULES_V5)
    v5_bez_zdi = evaluate_tendency(inputs(**base, spot=7410.0), version=TENDENCY_RULES_V5)
    v4 = evaluate_tendency(inputs(**base, **walls), version=TENDENCY_RULES_V4)
    assert v5 is not None and v5_bez_zdi is not None and v4 is not None
    walls_vote = next(vote for vote in v5.votes if vote.name == "walls_distance")
    assert walls_vote.vote == pytest.approx(0.8) and walls_vote.weight == 0.0
    assert v5.score == pytest.approx(v5_bez_zdi.score)  # nulová váha skóre nemění
    assert v5.weights_version == TENDENCY_RULES_V5
    # v4: táž poloha skóre posouvá a nese váhu 1
    assert v4.score > v5.score
    assert next(vote for vote in v4.votes if vote.name == "walls_distance").weight == 1.0
    assert v4.weights_version == TENDENCY_RULES_V4


def test_v5_minuta_jen_s_nulovou_vahou_nema_skore() -> None:
    """Stráž: hlasuje-li jen poloha mezi zdmi (v5 váha 0), výsledek je None, ne 0 / dělení nulou."""
    only_walls = inputs(call_wall=7500.0, put_wall=7400.0, spot=7410.0)
    assert evaluate_tendency(only_walls, version=TENDENCY_RULES_V5) is None
    v4 = evaluate_tendency(only_walls, version=TENDENCY_RULES_V4)
    assert v4 is not None and v4.score == pytest.approx(COMPONENT_CAP)


@pytest.mark.parametrize(
    ("minutes", "v4_votes", "v5_votes"),
    [
        (90.0, True, True),  # okraj okna T3
        (0.0, True, True),  # settle
        (-30.0, True, False),  # 30 min po settle: v4 hlasovala až do půlnoci UTC (#1584)
        (1200.0, False, False),  # 00:01 UTC nového dne: ~20 h do settle
    ],
)
def test_v5_max_pain_jen_0_az_90_min_do_settle(
    minutes: float, v4_votes: bool, v5_votes: bool
) -> None:
    case = inputs(max_pain=7460.0, minutes_to_close=minutes)
    names_v4 = {vote.name for vote in _collect_votes(case, TENDENCY_RULES_V4)}
    names_v5 = {vote.name for vote in _collect_votes(case, TENDENCY_RULES_V5)}
    assert ("max_pain" in names_v4) is v4_votes
    assert ("max_pain" in names_v5) is v5_votes


def _engine(prints: bool, tmp_path: Path | None = None) -> TendencyEngine:
    return TendencyEngine(
        symbol="ES",
        repository=None,  # type: ignore[arg-type]
        oi_repository=None,  # type: ignore[arg-type]
        publisher=None,  # type: ignore[arg-type]
        data_dir=tmp_path,
        cumdelta_prints=prints,
    )


def _feed(
    engine: TendencyEngine,
    start: dt.datetime,
    end: dt.datetime,
    flow: Mapping[dt.datetime, float | None] | None = None,
) -> tuple[float | None, float | None]:
    """Minuty [start, end] s flowΔ 5 (nebo z `flow`) a cenou 7400 + index; vrátí poslední."""
    result: tuple[float | None, float | None] = (None, None)
    ts = start
    index = 0
    while ts <= end:
        delta = (flow or {}).get(ts, 5.0)
        row = FlowRow(ts_min=ts, flow_delta=delta, cum_delta=None if delta is None else 0.0)
        result = engine._slope_inputs(ts, 7400.0 + index, row)
        ts += dt.timedelta(minutes=1)
        index += 1
    return result


@pytest.mark.parametrize(
    ("label", "open_utc"),
    [
        ("všední open, letní čas", dt.datetime(2026, 10, 14, 22, 0, tzinfo=dt.UTC)),
        ("všední open, zimní čas", dt.datetime(2026, 11, 4, 23, 0, tzinfo=dt.UTC)),
        ("nedělní open", dt.datetime(2026, 10, 18, 22, 0, tzinfo=dt.UTC)),
    ],
)
def test_v5_sklon_nepresahuje_zacatek_seance(label: str, open_utc: dt.datetime) -> None:
    """#1581, ADR-0057 bod 4: okno přes reset v 17:00 CT nehlasuje; první plné končí open + 9."""
    engine = _engine(prints=True)
    minute = dt.timedelta(minutes=1)
    slope, _ = _feed(engine, open_utc - 15 * minute, open_utc + 8 * minute)
    assert slope is None, label
    slope, price_then = engine._slope_inputs(
        open_utc + 9 * minute,
        7500.0,
        FlowRow(ts_min=open_utc + 9 * minute, flow_delta=5.0, cum_delta=0.0),
    )
    # (open − 1, open + 9]: 10 řádků nové seance po 5,0; cena hranice okna = open − 1
    assert slope == pytest.approx(50.0), label
    assert price_then == pytest.approx(7400.0 + 14), label


def test_v5_sklon_mezera_a_stary_radek_flow() -> None:
    """NULL v okně (mezera trade větve) i řádek z minulé minuty (pád cyklu) = sklon None."""
    start = dt.datetime(2026, 10, 15, 14, 0, tzinfo=dt.UTC)
    minute = dt.timedelta(minutes=1)
    engine = _engine(prints=True)
    slope, price_then = _feed(engine, start, start + 12 * minute, {start + 8 * minute: None})
    assert slope is None
    assert price_then == pytest.approx(7400.0 + 2)  # cena hranice okna rozchodu zůstává
    # Okno bez mezery dá součet; řádek s ts minulé minuty se nepřidá
    slope, _ = _feed(engine, start + 13 * minute, start + 19 * minute)
    assert slope == pytest.approx(50.0)
    stale = FlowRow(ts_min=start + 19 * minute, flow_delta=5.0, cum_delta=0.0)
    slope, _ = engine._slope_inputs(start + 20 * minute, 7500.0, stale)
    assert slope is None


def test_v5_pauza_dava_sklon_nula() -> None:
    """Za zavřeného trhu nese řada nuly: sklon 0 = měřený klid, ne None (ADR-0057 bod 2)."""
    start = dt.datetime(2026, 10, 15, 21, 0, tzinfo=dt.UTC)  # pauza 16:00–17:00 CT
    engine = _engine(prints=True)
    zeros = {start + dt.timedelta(minutes=i): 0.0 for i in range(15)}
    slope, _ = _feed(engine, start, start + dt.timedelta(minutes=14), zeros)
    assert slope == 0.0


def test_v4_sklon_beze_zmeny_rozdil_cum_delta() -> None:
    """Pod mechanikou v5 zůstává rozdíl CumΔ přes 10 min, i přes reset seance (#1581, v4)."""
    engine = _engine(prints=False)
    start = dt.datetime(2026, 10, 14, 21, 55, tzinfo=dt.UTC)
    result: tuple[float | None, float | None] = (None, None)
    for index in range(11):
        ts = start + dt.timedelta(minutes=index)
        cum = 1000.0 if ts < dt.datetime(2026, 10, 14, 22, 0, tzinfo=dt.UTC) else 10.0 * index
        result = engine._slope_inputs(
            ts, 7400.0 + index, FlowRow(ts_min=ts, flow_delta=0.0, cum_delta=cum)
        )
    slope, price_then = result
    assert slope == pytest.approx(100.0 - 1000.0)
    assert price_then == pytest.approx(7400.0)


def test_v5_hystereze_se_resetuje_otevrenim_seance() -> None:
    """#1584 bod 2: v5 reset v 17:00 CT (22:00 UTC v létě, 23:00 v zimě), ne v 00:00 UTC."""
    utc = dt.UTC
    v5 = _engine(prints=True)
    evening = v5._hysteresis_for(dt.datetime(2026, 10, 14, 21, 59, tzinfo=utc))
    after_open = v5._hysteresis_for(dt.datetime(2026, 10, 14, 22, 0, tzinfo=utc))
    assert after_open is not evening
    assert v5._hysteresis_for(dt.datetime(2026, 10, 15, 0, 0, tzinfo=utc)) is after_open
    winter = v5._hysteresis_for(dt.datetime(2026, 11, 4, 22, 30, tzinfo=utc))
    assert v5._hysteresis_for(dt.datetime(2026, 11, 4, 23, 0, tzinfo=utc)) is not winter
    # Neděle: víkend patří pondělní seanci až od nedělního openu
    saturday = v5._hysteresis_for(dt.datetime(2026, 10, 17, 12, 0, tzinfo=utc))
    sunday_open = v5._hysteresis_for(dt.datetime(2026, 10, 18, 22, 0, tzinfo=utc))
    assert sunday_open is not saturday
    # v4: reset v 00:00 UTC jako dosud
    v4 = _engine(prints=False)
    before = v4._hysteresis_for(dt.datetime(2026, 10, 14, 22, 0, tzinfo=utc))
    assert v4._hysteresis_for(dt.datetime(2026, 10, 14, 23, 59, tzinfo=utc)) is before
    assert v4._hysteresis_for(dt.datetime(2026, 10, 15, 0, 0, tzinfo=utc)) is not before


def test_v5_sentindex_hodnota_pred_oknem_ze_vcerejsi_partice(tmp_path: Path) -> None:
    """#1584 bod 3: okno přes půlnoc UTC najde hodnotu před oknem ve včerejší partici."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from gexlens_engine.tendency import SENTIMENT_SUBDIR

    folder = tmp_path / "derived" / SENTIMENT_SUBDIR / "ES"
    folder.mkdir(parents=True)
    utc = dt.UTC
    pq.write_table(
        pa.table(
            {
                "ts_min": [
                    dt.datetime(2026, 10, 15, 23, 50, tzinfo=utc),
                    dt.datetime(2026, 10, 15, 23, 58, tzinfo=utc),
                ],
                "value": [0.10, 0.20],
            }
        ),
        folder / "2026-10-15.parquet",
    )
    pq.write_table(
        pa.table({"ts_min": [dt.datetime(2026, 10, 16, 0, 1, tzinfo=utc)], "value": [0.30]}),
        folder / "2026-10-16.parquet",
    )
    now = dt.datetime(2026, 10, 16, 0, 5, tzinfo=utc)  # okno začíná 23:55 předchozího dne
    current, then = _engine(prints=True, tmp_path=tmp_path)._sentiment(now)
    assert current == pytest.approx(0.30)
    assert then == pytest.approx(0.10)
    current, then = _engine(prints=False, tmp_path=tmp_path)._sentiment(now)
    assert current == pytest.approx(0.30)
    assert then is None


@pytest.mark.parametrize(
    ("prints", "expected"),
    [
        # Pravidla v5 (mechanika v6): proti maximu seance, minuta po mezeře 0
        (True, [(None, None), (0.0, 0.25), (0.0, 0.0), (8.0, 0.0), (5.0, 0.25)]),
        # v4 beze změny (ADR-0057 bod 11): návrat 240 znovu, mezera celá v jedné minutě
        (False, [(None, None), (0.0, 0.25), (12.0, 0.0), (128.0, 0.25), (5.0, 0.25)]),
    ],
)
def test_tendence_flows_proti_maximu_seance(
    prints: bool, expected: list[tuple[float | None, float | None]]
) -> None:
    """#1602 AC: tytéž řady jako `setups._flows` (hlas `delta_flow`); první cyklus
    nic neměří a hlas vynechá."""
    engine = _engine(prints)
    runtime = QuotesRuntime()
    session = dt.date(2026, 10, 15)
    flows = [engine._flows(cast(EngineRuntime, runtime.step(index)), session) for index in range(5)]
    assert flows == expected


@pytest.mark.parametrize(("prints", "after"), [(True, (None, None)), (False, (210.0, 0.0))])
def test_tendence_flows_null_uprostred_seance_neprida_cely_kumulativ(
    prints: bool, after: tuple[float | None, float | None]
) -> None:
    """Fallback tasty (všechno NULL) a návrat IBKR: pod v5 tendence první minutu
    po mezeře vynechá; v4 by započetla celý výpadek (420 kontraktů · |0,5|)."""
    engine = _engine(prints)
    runtime = QuotesRuntime()
    call = next(iter(VOLUME_PLAN))
    session = dt.date(2026, 10, 15)

    def flows(volume: float | None) -> tuple[float | None, float | None]:
        runtime.quotes = {call: quote(volume, 0.5)}
        return engine._flows(cast(EngineRuntime, runtime), session)

    assert flows(1000.0) == (None, None)
    assert flows(1010.0) == (5.0, 0.0)
    assert flows(None) == (None, None)
    assert flows(1430.0) == after
    assert flows(1440.0) == (5.0, 0.0)


def test_bez_zive_ceny_se_hlasy_z_polohy_ceny_preskoci() -> None:
    """#1600 (pod v6): spot None — flip, zdi, Max Pain, těžiště a rozchod nehlasují,
    hlasy bez ceny (dominance zdí, sklon CumΔ) ano."""
    case = inputs(
        spot=None,
        flip=7400.0,
        call_wall=7500.0,
        put_wall=7300.0,
        call_wall_dom=0.4,
        put_wall_dom=0.6,
        max_pain=7450.0,
        centroid=7420.0,
        cum_delta_slope=120.0,
        price_then=7440.0,
        minutes_to_close=30.0,
    )
    names = {vote.name for vote in _collect_votes(case, TENDENCY_RULES_V5)}
    assert names == {"walls_dominance", "cum_delta_slope"}
    with_spot = {
        vote.name for vote in _collect_votes(replace(case, spot=7450.0), TENDENCY_RULES_V5)
    }
    assert {"flip", "walls_distance", "max_pain", "centroid", "divergence"} <= with_spot


class _UntouchedRuntime:
    """Runtime, na který se nesmí sáhnout — brána zavřeného trhu vrací dřív."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"runtime.{name} při zavřeném trhu")


@pytest.mark.parametrize(
    ("label", "ts"),
    [
        ("sobota", dt.datetime(2026, 10, 17, 15, 0, tzinfo=dt.UTC)),
        ("denní pauza 16:00–17:00 CT", dt.datetime(2026, 10, 15, 21, 30, tzinfo=dt.UTC)),
        ("neděle před otevřením", dt.datetime(2026, 10, 18, 21, 59, tzinfo=dt.UTC)),
    ],
)
async def test_v5_zavreny_trh_nepocita_ani_nezapisuje(label: str, ts: dt.datetime) -> None:
    """#1622: pravidla v5 (mechanika v6) při zavřeném trhu tendenci nepočítají."""
    engine = _engine(prints=True)
    await engine.on_minute(ts, 7450.0, cast(EngineRuntime, _UntouchedRuntime()))


@pytest.mark.parametrize(
    ("prints", "ts"),
    [
        (True, dt.datetime(2026, 10, 18, 22, 0, tzinfo=dt.UTC)),  # nedělní otevření, v5 počítá
        (False, dt.datetime(2026, 10, 17, 15, 0, tzinfo=dt.UTC)),  # v4 beze změny i v sobotu
    ],
)
async def test_otevreny_trh_nebo_v4_tendenci_pocita(prints: bool, ts: dt.datetime) -> None:
    engine = _engine(prints=prints)
    with pytest.raises(AssertionError, match="runtime"):
        await engine.on_minute(ts, 7450.0, cast(EngineRuntime, _UntouchedRuntime()))
