"""Testy walls módů (issue #16): golden testy, Ridge s více souběžnými hřebeny."""

import json
from pathlib import Path

import pytest

from gexlens_engine.compute.walls import (
    RidgeTrack,
    WallsMode,
    center_of_layer,
    compute_walls,
    local_maxima,
    peak_series,
    ridge_tracks,
    smooth_series,
)

GOLDEN_PATH = Path(__file__).parent / "golden" / "walls_basic.json"


def load_golden() -> dict[str, object]:
    data: dict[str, object] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return data


def golden_layers() -> list[dict[float, float]]:
    golden = load_golden()
    layers = golden["layers"]
    assert isinstance(layers, list)
    return [{float(k): v for k, v in layer.items()} for layer in layers]


def golden_expected() -> dict[str, object]:
    expected = load_golden()["expected"]
    assert isinstance(expected, dict)
    return expected


def test_golden_peak_series() -> None:
    assert peak_series(golden_layers()) == golden_expected()["peak"]


def test_golden_center() -> None:
    center = center_of_layer(golden_layers()[0])
    assert center == pytest.approx(golden_expected()["center_t0"])


def test_golden_smooth_ema() -> None:
    peaks = peak_series(golden_layers())
    smoothed = smooth_series(peaks, span=3)
    expected = golden_expected()["smooth_span3"]
    assert isinstance(expected, list)
    assert smoothed == pytest.approx(expected)


def test_golden_ridge_two_concurrent_tracks() -> None:
    """AC: Ridge vrací více souběžných hřebenů na multimodálním profilu."""
    tracks = ridge_tracks(golden_layers())

    expected_tracks = golden_expected()["ridge_tracks"]
    assert isinstance(expected_tracks, list)
    assert len(tracks) == 2
    strikes_per_track = sorted([point[1] for point in track.points] for track in tracks)
    assert strikes_per_track == sorted(t["strikes"] for t in expected_tracks)
    # Oba hřebeny běží souběžně přes všechny tři časy
    assert all(len(track.points) == 3 for track in tracks)


def test_prominence_filters_shallow_peak() -> None:
    case = load_golden()["prominence_case"]
    assert isinstance(case, dict)
    layer = {float(k): v for k, v in case["layer"].items()}

    assert local_maxima(layer, prominence_ratio=0.1) == case["expected_maxima"]
    # S vypnutým filtrem mělký vrchol zůstává
    assert local_maxima(layer, prominence_ratio=0.0) == [7550.0, 7650.0]


def test_ridge_tracks_split_and_new_track() -> None:
    # Hřeben se v čase t1 rozdvojí — nové maximum založí novou stopu
    layers: list[dict[float, float]] = [
        {7500.0: 1.0, 7550.0: 9.0, 7600.0: 1.0, 7650.0: 1.0},
        {7500.0: 1.0, 7550.0: 9.0, 7600.0: 1.0, 7650.0: 8.0},
    ]
    tracks = ridge_tracks(layers)
    assert len(tracks) == 2
    lengths = sorted(len(track.points) for track in tracks)
    assert lengths == [1, 2]  # původní hřeben pokračuje, nový začíná v t1


def test_ridge_respects_max_strike_gap() -> None:
    layers: list[dict[float, float]] = [
        {7500.0: 9.0, 7700.0: 1.0},
        {7500.0: 1.0, 7700.0: 9.0},  # maximum skočilo o 200 bodů
    ]
    unlimited = ridge_tracks(layers, max_strike_gap=None)
    limited = ridge_tracks(layers, max_strike_gap=50.0)
    assert len(unlimited) == 1  # bez limitu se stopy spojí
    assert len(limited) == 2  # s limitem vznikne nový hřeben


def test_smooth_handles_leading_none() -> None:
    assert smooth_series([None, 7600.0, 7610.0], span=3) == [None, 7600.0, 7605.0]
    with pytest.raises(ValueError, match="span"):
        smooth_series([7600.0], span=0)


def test_compute_walls_dispatcher() -> None:
    layers = golden_layers()
    flips: list[float | None] = [7580.0, None, 7590.0]

    assert compute_walls(WallsMode.PEAK, layers) == golden_expected()["peak"]
    assert compute_walls(WallsMode.FLIP, layers, flip_series=flips) == flips
    ridge = compute_walls(WallsMode.RIDGE, layers)
    assert all(isinstance(track, RidgeTrack) for track in ridge)
    with pytest.raises(ValueError, match="FLIP vyžaduje"):
        compute_walls(WallsMode.FLIP, layers)


def test_empty_and_zero_layers() -> None:
    assert peak_series([{}]) == [None]
    assert center_of_layer({}) is None
    assert local_maxima({}) == []
    assert peak_series([{7500.0: 0.0}]) == [None]  # nulová vrstva nemá peak
