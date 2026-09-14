"""Golden testy korekčních epizod SentIndexu (#565 fáze 2, ADR-0037).

Řada má řádek každý kalendářní den jako `sentiment_daily`; horizont H se
počítá v obchodních dnech (výchozí pondělí–pátek). Vedle pinnutých pravidel
test drží i paritu s měřicím skriptem `scripts/measure_sentiment_episodes.py`
(varianta A, zahlazení `peak`) — měření a produkce nesmí tiše utéct od sebe.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from gexlens_engine.compute.sentwaves import (
    EPISODE_ATTEMPT,
    EPISODE_NEGATION,
    EPISODE_NONE,
    EPISODE_OPEN,
    EPISODE_PARAMS_VERSION,
    EPISODE_THRESHOLD_D,
    DailyZ,
    assess_episode,
    correction_levels,
    detect_episodes,
    is_weekday,
    rolling_max_z,
)

START = dt.date(2026, 7, 28)  # úterý
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "measure_sentiment_episodes.py"


def series(z_values: Sequence[float | None], *, sigma: float = 2.0) -> list[DailyZ]:
    return [
        DailyZ(date=START + dt.timedelta(days=i), close=(z or 0.0) * sigma, z=z)
        for i, z in enumerate(z_values)
    ]


def trading_days_after(start: dt.date, count: int) -> dt.date:
    """Datum `count`-tého obchodního dne (po–pá) po `start`."""
    day = start
    seen = 0
    while seen < count:
        day += dt.timedelta(days=1)
        if is_weekday(day):
            seen += 1
    return day


def test_rolling_max_a_prah() -> None:
    points = series([0.0, 1.0, 0.5, None, 0.2])
    assert rolling_max_z(points, 2) == [0.0, 1.0, 1.0, None, None]
    assert correction_levels(points, threshold_d=1.0, window=2) == [-1.0, 0.0, 0.0, None, None]


def test_start_odjisteni_a_neprekryvani() -> None:
    # 25 dní 0, pád na −2 (start), −1.5 (bez odjištění), −0.5 (odjištění), −2 (nový start
    # možný až po rozhodnutí první — ta zatím probíhá → nezakládá se)
    points = series([0.0] * 25 + [-2.0, -1.5, -0.5, -2.0])
    episodes = detect_episodes(points, threshold_d=1.0, horizon_h=10)
    assert len(episodes) == 1
    assert episodes[0].start == START + dt.timedelta(days=25)
    assert episodes[0].end is None and episodes[0].label is None
    assert episodes[0].ref_level == 0.0
    assert episodes[0].depth_z == pytest.approx(2.0)
    # Práh 3 σ se nedosáhne
    assert detect_episodes(points, threshold_d=3.0) == []


def test_pokus_zahlazeni_nad_peak() -> None:
    points = series([0.0] * 25 + [-2.0, -1.0, 0.5] + [0.5] * 5)
    episodes = detect_episodes(points, threshold_d=1.0, horizon_h=10)
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.label == EPISODE_ATTEMPT
    assert episode.end == START + dt.timedelta(days=27)
    assert episode.depth_z == pytest.approx(2.0)
    start = START + dt.timedelta(days=25)
    assert episode.length_days == sum(
        1 for i in range(26, 28) if is_weekday(START + dt.timedelta(days=i))
    )
    assert episode.start == start


def test_negace_po_h_obchodnich_dnech() -> None:
    points = series([0.0] * 25 + [-2.0] * 30)
    episodes = detect_episodes(points, threshold_d=1.0, horizon_h=5)
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.label == EPISODE_NEGATION
    assert episode.length_days == 5
    assert episode.end == trading_days_after(START + dt.timedelta(days=25), 5)
    # Po negaci se nová epizoda založí až po odjištění: zde drawdown trvá → žádná další
    assert detect_episodes(points, threshold_d=1.0, horizon_h=5) == episodes


def test_dalsi_epizoda_po_rozhodnuti_a_odjisteni() -> None:
    # negace (H=5), pak návrat nad práh (odjištění, ale ne nad peak), pak nový pád
    z = [0.0] * 25 + [-2.0] * 10 + [-0.5] * 3 + [-2.0] * 3
    episodes = detect_episodes(series(z), threshold_d=1.0, horizon_h=5)
    assert [e.label for e in episodes] == [EPISODE_NEGATION, None]
    assert episodes[1].start == START + dt.timedelta(days=38)


def test_po_zahlazeni_je_odjisteno_hned() -> None:
    # Zahlazení = nové maximum (drawdown 0) → pád hned další den zakládá novou epizodu
    z = [0.0] * 25 + [-2.0, 0.5, -1.0]
    episodes = detect_episodes(series(z), threshold_d=1.0, horizon_h=10)
    assert [e.label for e in episodes] == [EPISODE_ATTEMPT, None]
    assert episodes[1].start == START + dt.timedelta(days=27)
    assert episodes[1].ref_level == pytest.approx(0.5)


def test_vlastni_obchodni_kalendar() -> None:
    # S kalendářem „každý den je obchodní" je horizont v kalendářních dnech
    points = series([0.0] * 25 + [-2.0] * 30)
    episode = detect_episodes(points, horizon_h=5, is_trading_day=lambda _d: True)[0]
    assert episode.end == START + dt.timedelta(days=30)


def test_assess_episode_stavy() -> None:
    # none: žádný pokles
    flat = assess_episode(series([0.0] * 30))
    assert flat.status == EPISODE_NONE and flat.episode is None
    assert flat.correction_threshold == pytest.approx(-EPISODE_THRESHOLD_D)
    assert flat.params_version == EPISODE_PARAMS_VERSION
    # open: probíhá
    open_state = assess_episode(series([0.0] * 25 + [-2.0, -1.8]))
    assert open_state.status == EPISODE_OPEN
    assert open_state.episode is not None and open_state.episode.length_days == (
        1 if is_weekday(START + dt.timedelta(days=26)) else 0
    )
    # attempt: jen v den zahlazení, den poté none (korekce je pryč)
    attempt_today = assess_episode(series([0.0] * 25 + [-2.0, 0.5]))
    assert attempt_today.status == EPISODE_ATTEMPT
    after = assess_episode(series([0.0] * 25 + [-2.0, 0.5, 0.4]))
    assert after.status == EPISODE_NONE
    assert after.last_resolved is not None and after.last_resolved.label == EPISODE_ATTEMPT
    # negation: trvá, dokud se close_z nevrátí nad referenční úroveň
    negation = assess_episode(series([0.0] * 25 + [-2.0] * 20 + [-0.5] * 3), horizon_h=5)
    assert negation.status == EPISODE_NEGATION
    recovered = assess_episode(series([0.0] * 25 + [-2.0] * 20 + [0.3]), horizon_h=5)
    assert recovered.status == EPISODE_NONE
    assert recovered.last_resolved is not None
    assert recovered.last_resolved.label == EPISODE_NEGATION
    # Práh = 20denní maximum close_z − D (okno končí třemi dny na −0,5)
    assert negation.correction_threshold == pytest.approx(-0.5 - EPISODE_THRESHOLD_D)


def _script() -> Any:
    spec = importlib.util.spec_from_file_location("measure_sentiment_episodes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parita_s_mericim_skriptem() -> None:
    """Varianta A + `peak` skriptu = produkční detect_episodes (stejné starty,
    třídy a hloubky) nad řadou s několika korekcemi; obchodní dny = po–pá."""
    script = _script()
    z: Sequence[float | None] = (
        [0.0] * 25 + [-2.0, -1.0, 0.5, -0.9] + [0.3] * 3 + [-1.5] * 14 + [-0.2] * 4 + [-3.0, -2.5]
    )
    points = series(z)
    horizon_h = 10
    engine_eps = detect_episodes(points, threshold_d=1.0, horizon_h=horizon_h)
    daily = [(p.date, p.close, p.z, 2.0) for p in points]
    closes = {p.date: 100.0 for p in points if is_weekday(p.date)}
    script_eps = script.detect_episodes(
        "ES",
        script.enrich(daily),
        closes,
        variant=script.VARIANT_DRAWDOWN,
        threshold_d=1.0,
        horizon_h=horizon_h,
        era_start=START,
        recovery=script.RECOVERY_PEAK,
    )
    assert len(engine_eps) == len(script_eps) >= 3
    for mine, theirs in zip(engine_eps, script_eps, strict=True):
        assert mine.start == theirs.start
        assert mine.end == theirs.end
        assert mine.label == theirs.label
        assert mine.depth_z == pytest.approx(theirs.depth_z)
        assert mine.ref_level == pytest.approx(theirs.ref_level)
