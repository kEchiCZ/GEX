"""Golden testy stavových pravidel SPEC 5.6 (#292, kap. 10)."""

import datetime as dt

import pytest

from gexlens_engine.compute.sentwaves import (
    DailyClose,
    assess_state,
    confirmation_threshold,
    day_condition,
    detect_waves,
    moving_average,
    position_state,
    trend_polarity,
)

START = dt.date(2026, 7, 1)


def series(closes: list[float]) -> list[DailyClose]:
    return [
        DailyClose(date=START + dt.timedelta(days=i), close=close) for i, close in enumerate(closes)
    ]


# ── Stavební bloky ─────────────────────────────────────────────────


def test_moving_average_needs_full_window() -> None:
    assert moving_average([1.0, 2.0], 5) is None
    assert moving_average([1.0, 2.0, 3.0, 4.0, 5.0], 5) == pytest.approx(3.0)
    # Bere posledních N, ne prvních
    assert moving_average([0.0, 0.0, 0.0, 0.0, 0.0, 5.0], 5) == pytest.approx(1.0)


def test_day_condition_pinned_rules() -> None:
    """SPEC 5.6: RiskOn ⇔ close > MA5 > MA10; zrcadlově RiskOff; jinak nic."""
    assert day_condition(1.0, 0.5, 0.2) == "RiskOn"
    assert day_condition(-1.0, -0.5, -0.2) == "RiskOff"
    assert day_condition(1.0, 0.2, 0.5) is None  # MA5 < MA10 → žádný RiskOn
    assert day_condition(0.3, 0.5, 0.2) is None  # close pod MA5
    assert day_condition(1.0, None, None) is None  # okno MA není plné
    # Rovnost není ostrá nerovnost — podmínka nesmí platit
    assert day_condition(0.5, 0.5, 0.2) is None


# ── Detekce vln (ručně spočtená golden řada) ───────────────────────
# 10× 0.0 → MA5=MA10=0. Den 11: close 1 → MA5=0.2, MA10=0.1 → RiskOn,
# |1−0.1|=0.9. Den 12: close 1 → MA5=0.4, MA10=0.2 → RiskOn, |1−0.2|=0.8.
# Den 13: close 0 → MA5=(0+0+0+1+1)/5=0.4 → 0 > 0.4 neplatí → vlna končí
# dnem 12 s hloubkou max(0.9, 0.8) = 0.9 a délkou 2.
GOLDEN = [0.0] * 10 + [1.0, 1.0, 0.0]


def test_detect_waves_golden_depth_and_bounds() -> None:
    waves = detect_waves(series(GOLDEN))
    assert len(waves) == 1
    wave = waves[0]
    assert wave.direction == "RiskOn"
    assert wave.start == START + dt.timedelta(days=10)
    assert wave.end == START + dt.timedelta(days=11)
    assert wave.depth == pytest.approx(0.9)
    assert wave.length_days == 2


def test_detect_waves_ongoing_has_no_end() -> None:
    waves = detect_waves(series([0.0] * 10 + [1.0, 1.0]))
    assert len(waves) == 1
    assert waves[0].end is None  # podmínka platí i poslední den → probíhá


def test_detect_waves_direction_switch_closes_previous() -> None:
    # RiskOn vlna, pak propad do RiskOff bez neutrálního dne mezi tím
    closes = [0.0] * 10 + [1.0, 1.0, -2.0, -2.0, -2.0]
    waves = detect_waves(series(closes))
    directions = [w.direction for w in waves]
    assert directions[0] == "RiskOn"
    assert "RiskOff" in directions
    on = waves[0]
    off = next(w for w in waves if w.direction == "RiskOff")
    # Vlny se nepřekrývají a RiskOff začíná po konci RiskOn
    assert on.end is not None and off.start > on.end


def test_mirror_symmetry() -> None:
    """RiskOff je zrcadlo RiskOn — stejná řada s opačným znaménkem."""
    up = detect_waves(series(GOLDEN))
    down = detect_waves(series([-c for c in GOLDEN]))
    assert len(up) == len(down) == 1
    assert down[0].direction == "RiskOff"
    assert down[0].depth == pytest.approx(up[0].depth)
    assert down[0].start == up[0].start and down[0].end == up[0].end


# ── Potvrzovací práh (walk-forward) ────────────────────────────────


def test_threshold_uses_only_opposite_waves_completed_before() -> None:
    waves = detect_waves(series(GOLDEN))  # RiskOn, end den 11, hloubka 0.9
    after = START + dt.timedelta(days=20)
    # Práh RiskOff = průměr hloubek RiskOn vln dokončených před začátkem
    assert confirmation_threshold(waves, direction="RiskOff", before=after) == pytest.approx(0.9)
    # Vlna vlastního směru práh netvoří
    assert confirmation_threshold(waves, direction="RiskOn", before=after) == 0.0
    # Walk-forward: vlna dokončená POZDĚJI nesmí kalibrovat dřívější stav
    before_wave = START + dt.timedelta(days=5)
    assert confirmation_threshold(waves, direction="RiskOff", before=before_wave) == 0.0


# ── Stav (pinnutá pravidla) ────────────────────────────────────────


def test_state_neutral_without_full_ma_window() -> None:
    assessment = assess_state(series([1.0, 2.0, 3.0]))
    assert assessment.state == "Neutral"
    assert assessment.ma10 is None


def test_position_state_pinned_rules() -> None:
    """SPEC 5.6 rev. #563: RiskOn = nad oběma, RiskOff = pod oběma, mezi = Neutral."""
    assert position_state(1.0, 0.5, 0.2) == "RiskOn"
    assert position_state(1.0, 0.2, 0.5) == "RiskOn"  # polarita MA stav nemění
    assert position_state(-1.0, -0.5, -0.2) == "RiskOff"
    assert position_state(0.3, 0.5, 0.2) == "Neutral"  # mezi průměry
    assert position_state(0.3, 0.2, 0.5) == "Neutral"
    assert position_state(1.0, None, None) is None  # okno MA není plné
    # Rovnost není ostrá nerovnost — polohu nepotvrzuje
    assert position_state(0.5, 0.5, 0.2) == "Neutral"


def test_trend_polarity() -> None:
    assert trend_polarity(0.5, 0.2) == "up"
    assert trend_polarity(0.2, 0.5) == "down"
    assert trend_polarity(0.5, 0.5) is None
    assert trend_polarity(None, 0.5) is None


def test_state_defined_every_day_with_full_windows() -> None:
    """Jádro #563: stav je definovaný každý den — vlnová brána ho už negatuje.

    Dřívější pravidla by tu vrátila Neutral (mělká vlna pod prahem);
    poloha pod oběma průměry je ale RiskOff bez ohledu na hloubku.
    """
    base = [0.0] * 10 + [1.0, 1.0] + [0.0] * 10
    shallow = assess_state(series(base + [-0.1]))
    assert shallow.state == "RiskOff"  # close −0.1 pod MA5 −0.02 i MA10 −0.01
    assert shallow.polarity == "down"
    # Vlna a práh zůstávají jako atributy (kalibrace/UI)
    assert shallow.wave is not None and shallow.wave.direction == "RiskOff"
    assert shallow.threshold == pytest.approx(0.9)

    deep = assess_state(series(base + [-3.0]))
    assert deep.state == "RiskOff"
    assert deep.threshold == pytest.approx(0.9)


def test_state_riskon_without_wave_history() -> None:
    assessment = assess_state(series([0.0] * 10 + [1.0]))
    assert assessment.state == "RiskOn"
    assert assessment.polarity == "up"
    assert assessment.threshold == 0.0
    assert assessment.wave is not None and assessment.wave.end is None


def test_state_between_averages_is_neutral() -> None:
    # GOLDEN končí close 0.0: MA5 = 0.4, MA10 = 0.2 → mezi? 0.0 < obě → RiskOff.
    # Proto vlastní řada: poslední close mezi průměry.
    assessment = assess_state(series([0.0] * 10 + [1.0, 1.0, 0.3]))
    assert assessment.ma5 is not None and assessment.ma10 is not None
    assert min(assessment.ma5, assessment.ma10) < 0.3 < max(assessment.ma5, assessment.ma10)
    assert assessment.state == "Neutral"
    assert assessment.wave is None  # řetězená podmínka padla → žádná probíhající vlna


def test_empty_series_is_neutral() -> None:
    assessment = assess_state([])
    assert assessment.state == "Neutral"
    assert assessment.close is None
