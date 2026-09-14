"""Testy skriptu měření korekčních epizod (#565 fáze 1) nad syntetickou řadou.

Skript žije ve `scripts/`, načítá se přes importlib jako u migrace news_reactions.
Řada sentimentu má řádek každý kalendářní den (jako `sentiment_daily`), podklad
jen v pracovní dny — horizont H se počítá v obchodních dnech podkladu.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "measure_sentiment_episodes.py"
DAY0 = dt.date(2026, 7, 28)  # úterý; shodou okolností začátek živé éry


@pytest.fixture(scope="module")
def mod() -> Any:
    spec = importlib.util.spec_from_file_location("measure_sentiment_episodes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses s `from __future__ import annotations` hledají modul v sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def daily_rows(z_values: list[float], *, sigma: float = 2.0) -> list[Any]:
    """Řádky (date, close, close_z, σ) den po dni; close = z·σ (MA10 nad surovými)."""
    return [(DAY0 + dt.timedelta(days=i), z * sigma, z, sigma) for i, z in enumerate(z_values)]


def weekday_closes(days: int, *, price: float = 100.0, step: float = 0.0) -> dict[dt.date, float]:
    """Close podkladu v pracovních dnech; `step` = denní změna v bodech."""
    closes: dict[dt.date, float] = {}
    value = price
    for i in range(days):
        day = DAY0 + dt.timedelta(days=i)
        if day.weekday() < 5:
            closes[day] = value
            value += step
    return closes


def test_rolling_max_vcetne_aktualniho_dne_a_mezery(mod: Any) -> None:
    assert mod.rolling_max([1.0, 3.0, 2.0, 0.0], 2) == [1.0, 3.0, 3.0, 2.0]
    assert mod.rolling_max([1.0, None, 2.0], 2) == [1.0, None, None]


def test_drawdown_start_a_odjisteni(mod: Any) -> None:
    # 25 dní plochých 0, pád na −2 (start), návrat na −0.5 (odjištění pro D=1),
    # znovu pád na −2 (druhý start); mezi tím drawdown ≥ 1 bez odjištění = žádný start
    z = [0.0] * 25 + [-2.0, -1.5, -1.2, -0.5, -2.0]
    points = mod.enrich(daily_rows(z))
    starts = mod.episode_starts(points, mod.VARIANT_DRAWDOWN, 1.0)
    assert [(i, ref) for i, ref in starts] == [(25, 0.0), (29, 0.0)]
    # Práh 3 σ se nikdy nedosáhne
    assert mod.episode_starts(points, mod.VARIANT_DRAWDOWN, 3.0) == []


def test_riskoff_start_jen_pri_vstupu_do_stavu(mod: Any) -> None:
    # Rostoucí řada (RiskOn), pak tři dny hluboko pod oběma MA (RiskOff) — jediný start
    z = [float(i) * 0.1 for i in range(15)] + [-5.0, -5.0, -5.0]
    points = mod.enrich(daily_rows(z))
    assert [p.state for p in points[-3:]] == [mod.RISK_OFF] * 3
    starts = mod.episode_starts(points, mod.VARIANT_RISKOFF, None)
    assert [i for i, _ in starts] == [15]


def test_pokus_vs_negace_podle_pravidla_zahlazeni(mod: Any) -> None:
    # Start den 25, pád na −2, den 27 zpět na +0.5 nad peak (0) → pokus
    z = [0.0] * 25 + [-2.0, -1.0, 0.5] + [0.5] * 20
    points = mod.enrich(daily_rows(z))
    closes = weekday_closes(len(z))
    trading_days = sorted(closes)
    end, label, depth, length = mod.resolve_episode(
        points, 25, 0.0, 10, trading_days, recovery=mod.RECOVERY_PEAK
    )
    assert label == mod.ATTEMPT
    assert end == DAY0 + dt.timedelta(days=27)
    assert depth == pytest.approx(2.0)
    assert length == len([d for d in trading_days if DAY0 + dt.timedelta(days=25) < d <= end])

    # Bez návratu nad peak → negace přesně po H obchodních dnech
    z_neg = [0.0] * 25 + [-2.0] * 30
    points_neg = mod.enrich(daily_rows(z_neg))
    closes_neg = weekday_closes(len(z_neg))
    end, label, depth, length = mod.resolve_episode(
        points_neg, 25, 0.0, 5, sorted(closes_neg), recovery=mod.RECOVERY_PEAK
    )
    assert label == mod.NEGATION
    assert length == 5
    after = [d for d in sorted(closes_neg) if d > DAY0 + dt.timedelta(days=25)]
    assert end == after[4]

    # Pravidlo `either`: stačí close_z nad hodnotou z dne začátku (−2 → −1.9)
    z_bounce = [0.0] * 25 + [-2.0, -1.9] + [-1.9] * 30
    points_bounce = mod.enrich(daily_rows(z_bounce))
    end, label, _, _ = mod.resolve_episode(
        points_bounce, 25, 0.0, 10, sorted(weekday_closes(len(z_bounce))), recovery="either"
    )
    assert label == mod.ATTEMPT and end == DAY0 + dt.timedelta(days=26)
    end, label, _, _ = mod.resolve_episode(
        points_bounce, 25, 0.0, 10, sorted(weekday_closes(len(z_bounce))), recovery="peak"
    )
    assert label == mod.NEGATION


def test_cenzura_bez_dat_do_horizontu(mod: Any) -> None:
    z = [0.0] * 25 + [-2.0, -2.0]
    points = mod.enrich(daily_rows(z))
    closes = weekday_closes(len(z))
    end, label, _, _ = mod.resolve_episode(points, 25, 0.0, 10, sorted(closes))
    assert end is None and label is None


def test_forward_returns_od_posledniho_close_a_cenzura(mod: Any) -> None:
    closes = weekday_closes(40, price=100.0, step=1.0)
    trading_days = sorted(closes)
    # Kotva o víkendu → základ = pátek; 5 obchodních dní = +5 bodů z ceny pátku
    saturday = next(d for d in (DAY0 + dt.timedelta(days=i) for i in range(10)) if d.weekday() == 5)
    fwd = mod.forward_returns(closes, trading_days, saturday)
    friday_close = closes[saturday - dt.timedelta(days=1)]
    assert fwd[5] == pytest.approx(5.0 / friday_close * 100.0)
    # 20 dní za koncem dat → None
    assert mod.forward_returns(closes, trading_days, trading_days[-3])[20] is None


def test_detect_episodes_neprekryva_a_respektuje_eru(mod: Any) -> None:
    # Dvě korekce: první (den 25) negace s H=5, druhá by začala den 28 (uvnitř první) → přeskočí se
    z = [0.0] * 25 + [-2.0, -0.5, -0.4, -2.0] + [-2.0] * 30
    points = mod.enrich(daily_rows(z))
    closes = weekday_closes(len(z))
    episodes = mod.detect_episodes(
        "ES",
        points,
        closes,
        variant=mod.VARIANT_DRAWDOWN,
        threshold_d=1.0,
        horizon_h=5,
        era_start=DAY0,
    )
    assert [e.start for e in episodes] == [DAY0 + dt.timedelta(days=25)]
    assert episodes[0].label == mod.NEGATION
    # Éra začínající až po startu epizody → epizoda se nevykáže
    assert (
        mod.detect_episodes(
            "ES",
            points,
            closes,
            variant=mod.VARIANT_DRAWDOWN,
            threshold_d=1.0,
            horizon_h=5,
            era_start=DAY0 + dt.timedelta(days=26),
        )
        == []
    )


def make_episode(mod: Any, label: str | None, fwd10: float | None, start: dt.date) -> Any:
    return mod.Episode(
        symbol="ES",
        variant="A",
        recovery="peak",
        threshold_d=1.0,
        horizon_h=10,
        start=start,
        ref_level=0.0,
        end=start,
        label=label,
        depth_z=2.0,
        length_days=1,
        fwd_from_resolution={5: fwd10, 10: fwd10, 20: fwd10},
        fwd_from_start={5: fwd10, 10: fwd10, 20: fwd10},
    )


def test_balanced_accuracy_a_summarize(mod: Any) -> None:
    episodes = [
        make_episode(mod, mod.ATTEMPT, 1.0, DAY0),
        make_episode(mod, mod.ATTEMPT, -1.0, DAY0),
        make_episode(mod, mod.NEGATION, -1.0, DAY0),
        make_episode(mod, mod.NEGATION, -2.0, DAY0),
        make_episode(mod, None, None, DAY0),
    ]
    # pokusy: 1 ze 2 roste (0.5); negace: 2 ze 2 klesají (1.0) → 0.75
    assert mod.balanced_accuracy(episodes, 10) == pytest.approx(0.75)
    assert mod.balanced_accuracy(episodes[:2], 10) is None  # chybí druhá třída
    cell = mod.summarize("ES", "A", 1.0, 10, episodes)
    assert (cell.attempts.n, cell.negations.n, cell.censored) == (2, 2, 1)
    assert cell.negations.fwd_mean[10] == pytest.approx(-1.5)
    assert cell.attempts.fwd_positive[10] == pytest.approx(0.5)


def test_walk_forward_okna_a_baseline_pri_shode(mod: Any) -> None:
    trading_days = sorted(weekday_closes(60))
    # Kandidát (0.5, 5) má v IS okně obě třídy a BA 1.0; baseline žádnou negaci → kandidát vyhraje
    is_day = trading_days[3]
    by_candidate: dict[tuple[float, int], list[Any]] = {c: [] for c in mod.grid_candidates()}
    by_candidate[(0.5, 5)] = [
        make_episode(mod, mod.ATTEMPT, 1.0, is_day),
        make_episode(mod, mod.NEGATION, -1.0, is_day),
    ]
    by_candidate[mod.BASELINE] = [make_episode(mod, mod.ATTEMPT, 1.0, is_day)]
    folds = mod.walk_forward(by_candidate, trading_days)
    assert folds, "60 pracovních dnů musí dát aspoň jeden fold"
    assert folds[0].chosen == (0.5, 5)
    assert folds[0].in_sample == (trading_days[0], trading_days[mod.IN_SAMPLE])
    # Při shodě (oba BA 1.0) vyhrává baseline
    by_candidate[mod.BASELINE].append(make_episode(mod, mod.NEGATION, -1.0, is_day))
    assert mod.walk_forward(by_candidate, trading_days)[0].chosen == mod.BASELINE
    # Víkendový start mezi OOS okny propadá do okna, které víkend obsahuje
    oos = folds[0].out_sample
    saturday = next(
        oos[0] + dt.timedelta(days=i)
        for i in range(7)
        if (oos[0] + dt.timedelta(days=i)).weekday() == 5
    )
    assert mod.in_window(make_episode(mod, mod.ATTEMPT, 1.0, saturday), oos)


def test_measure_symbol_a_render_nad_syntetickou_radou(mod: Any) -> None:
    z = [0.0] * 30 + [-2.0, -1.0, 0.5] + [0.5] * 10 + [-3.0] * 30
    daily = daily_rows(z)
    closes = weekday_closes(len(z), step=0.5)
    result = mod.measure_symbol("ES", daily, closes, era="live")
    assert [r.recovery for r in result.rules] == list(mod.RECOVERY_RULES)
    peak = result.rules[0]
    assert len(peak.cells) == len(mod.GRID_D) * len(mod.GRID_H) + len(mod.GRID_H)
    baseline_cell = next(
        c for c in peak.cells if c.variant == "A" and c.threshold_d == 1.0 and c.horizon_h == 10
    )
    assert baseline_cell.attempts.n == 1 and baseline_cell.negations.n == 1
    report = mod.render_markdown([result], today=dt.date(2026, 9, 14))
    assert "## ES — živá éra" in report
    assert "| A | 1 | 10 | 1 | 1 | 0 |" in report
    with pytest.raises(ValueError):
        mod.measure_symbol("ES", daily, closes, era="nope")
