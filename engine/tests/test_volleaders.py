"""Testy detektoru vol koncentrace (#208): dominance, podlahy, málo dat."""

import pytest

from gexlens_engine.compute.volleaders import detect_concentration


def volumes(**sides: float) -> dict[tuple[float, str], float]:
    """Pomocník: c7500=100 → {(7500.0, 'C'): 100.0}."""
    return {(float(key[1:]), key[0].upper()): value for key, value in sides.items()}


def test_dominant_side_detected() -> None:
    data = volumes(p7450=4100, p7500=900, c7580=800, p7400=700, c7600=600)
    found = detect_concentration(data, ratio=3.0, min_volume=500)
    assert found is not None
    assert (found.strike, found.right) == (7450.0, "P")
    assert found.volume == 4100
    assert found.median_top == 800  # medián [4100, 900, 800, 700, 600]
    assert found.ratio == pytest.approx(4100 / 800)


def test_flat_profile_no_alert() -> None:
    data = volumes(p7450=1000, p7500=900, c7580=950, p7400=850)
    assert detect_concentration(data, ratio=3.0, min_volume=500) is None


def test_min_volume_floor_blocks_morning_noise() -> None:
    # Poměr přestřelený (30×), ale absolutně jde o pár kontraktů
    data = volumes(p7450=300, p7500=10, c7580=10, p7400=8)
    assert detect_concentration(data, ratio=3.0, min_volume=500) is None
    assert detect_concentration(data, ratio=3.0, min_volume=100) is not None


def test_too_few_sides_no_median() -> None:
    data = volumes(p7450=5000, c7500=100)
    assert detect_concentration(data, ratio=3.0, min_volume=500) is None


def test_zero_volumes_ignored() -> None:
    data = volumes(p7450=4000, p7500=800, c7580=700) | {(7600.0, "C"): 0.0}
    found = detect_concentration(data, ratio=3.0, min_volume=500)
    assert found is not None and found.strike == 7450.0
