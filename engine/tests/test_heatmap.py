"""Testy heatmap metrik (issue #15): golden test každého módu, škály, normalizace, čistota."""

import json
import math
from pathlib import Path

import pytest

from gexlens_engine.compute.heatmap import (
    HeatmapCell,
    HeatmapMode,
    HeatmapScale,
    apply_scale,
    compute_mode,
    normalize,
    p99_denominator,
)

GOLDEN_PATH = Path(__file__).parent / "golden" / "heatmap_basic.json"


def load_golden() -> tuple[list[HeatmapCell], float, dict[str, dict[str, dict[str, float]]]]:
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    cells = [HeatmapCell(**cell) for cell in data["cells"]]
    return cells, data["spot"], data["expected"]


@pytest.mark.parametrize("mode", list(HeatmapMode))
def test_golden_every_mode(mode: HeatmapMode) -> None:
    """AC: každý mód má golden test s ručně spočtenými hodnotami."""
    cells, spot, expected = load_golden()

    layers = compute_mode(cells, mode, spot)

    golden_layers = expected[mode.value]
    assert set(layers) == set(golden_layers)
    for layer_name, golden in golden_layers.items():
        computed = layers[layer_name]
        assert set(computed) == {float(k) for k in golden}
        for strike, value in golden.items():
            assert computed[float(strike)] == pytest.approx(value), (
                f"{mode.value}/{layer_name}@{strike}"
            )


def test_mode_switch_is_pure_no_mutation() -> None:
    """AC: přepnutí módu = čistá funkce nad snapshot maticí (bez I/O, bez mutace vstupu)."""
    cells, spot, _ = load_golden()
    snapshot_before = list(cells)

    for mode in HeatmapMode:
        compute_mode(cells, mode, spot)

    assert cells == snapshot_before  # vstup nezměněn, žádný skrytý stav


def test_scales_preserve_sign() -> None:
    layer = {7590.0: 30.0, 7600.0: -40.0, 7610.0: 60.0}

    linear = apply_scale(layer, HeatmapScale.LINEAR)
    sqrt = apply_scale(layer, HeatmapScale.SQRT)
    log = apply_scale(layer, HeatmapScale.LOG)
    cbrt = apply_scale(layer, HeatmapScale.CBRT)

    assert linear == layer
    assert sqrt[7590.0] == pytest.approx(math.sqrt(30.0))
    assert sqrt[7600.0] == pytest.approx(-math.sqrt(40.0))
    assert log[7610.0] == pytest.approx(math.log1p(60.0))
    assert log[7600.0] == pytest.approx(-math.log1p(40.0))
    assert cbrt[7610.0] == pytest.approx(60.0 ** (1.0 / 3.0))
    assert cbrt[7600.0] == pytest.approx(-(40.0 ** (1.0 / 3.0)))


def test_p99_normalization_robust_to_outlier() -> None:
    values = [1.0] * 99 + [1000.0]  # jeden outlier ze 100 hodnot
    assert p99_denominator(values) == 1.0  # outlier nad p99 normalizaci neřídí

    tiny_window = [1.0, 2.0, 3.0]
    assert p99_denominator(tiny_window) == 3.0  # malé okno → p99 ≈ max


def test_normalize_by_p99_and_global_max() -> None:
    layer = {7590.0: 30.0, 7600.0: -40.0, 7610.0: 60.0}

    window = normalize(layer, p99_denominator(list(layer.values())))
    global_scaled = normalize(layer, 120.0)  # „globální max" dodá volající

    assert window[7610.0] == pytest.approx(1.0)
    assert window[7600.0] == pytest.approx(-40.0 / 60.0)
    assert global_scaled[7610.0] == pytest.approx(0.5)


def test_normalize_zero_denominator_gives_zero_layer() -> None:
    layer = {7590.0: 30.0}
    assert normalize(layer, 0.0) == {7590.0: 0.0}
    assert p99_denominator([]) == 0.0


def test_oi_plus_otm_weights_configurable() -> None:
    cells, spot, _ = load_golden()
    layers = compute_mode(cells, HeatmapMode.OI_PLUS_OTM, spot, oi_weight=1.0, vol_weight=0.0)
    # Čistě OI složka normalizovaná maximem 200
    assert layers["call"][7600.0] == pytest.approx(150.0 / 200.0)
    assert layers["put"][7590.0] == pytest.approx(1.0)
