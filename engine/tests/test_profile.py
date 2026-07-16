"""Testy strike profilu (issue #18): golden test, varianty, oddělené složky."""

import json
from pathlib import Path

import pytest

from gexlens_engine.compute.profile import ProfileInput, ProfileVariant, compute_profile

GOLDEN_PATH = Path(__file__).parent / "golden" / "profile_basic.json"


def load_golden() -> tuple[list[ProfileInput], float, float, dict[str, dict[str, float]]]:
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    inputs = [ProfileInput(**item) for item in data["inputs"]]
    return inputs, data["spot"], data["oi_weight"], data["expected"]


def test_golden_components_separated() -> None:
    """AC: výstup obsahuje složky odděleně (pro skládané pruhy v UI)."""
    inputs, spot, weight, expected = load_golden()

    profiles = compute_profile(inputs, ProfileVariant.COMBINED, spot, oi_weight=weight)

    assert len(profiles) == len(expected)
    for row in profiles:
        golden = expected[str(row.strike)]
        assert row.call_vol_component == pytest.approx(golden["call_vol_component"])
        assert row.call_oi_component == pytest.approx(golden["call_oi_component"])
        assert row.put_vol_component == pytest.approx(golden["put_vol_component"])
        assert row.put_oi_component == pytest.approx(golden["put_oi_component"])
        assert row.net == pytest.approx(golden["net_combined"])
        assert row.distance_from_spot == pytest.approx(golden["distance_from_spot"])


@pytest.mark.parametrize(
    ("variant", "key"),
    [
        (ProfileVariant.VOL, "net_vol"),
        (ProfileVariant.OI_DELTA, "net_oi_delta"),
        (ProfileVariant.COMBINED, "net_combined"),
    ],
)
def test_golden_variants(variant: ProfileVariant, key: str) -> None:
    """Varianty dropdownu Vol / OI Δ / kombinace (SPEC 4.6)."""
    inputs, spot, weight, expected = load_golden()

    profiles = compute_profile(inputs, variant, spot, oi_weight=weight)

    for row in profiles:
        assert row.net == pytest.approx(expected[str(row.strike)][key]), variant.value


def test_tooltip_raw_values_present() -> None:
    inputs, spot, weight, _ = load_golden()
    profiles = compute_profile(inputs, ProfileVariant.COMBINED, spot, oi_weight=weight)

    row = next(r for r in profiles if r.strike == 7590.0)
    assert row.call_volume == 40.0
    assert row.put_volume == 10.0
    assert row.call_oi == 100.0
    assert row.put_oi == 200.0


def test_one_sided_strike_and_invalid_right() -> None:
    only_call = [ProfileInput(strike=7600.0, right="C", volume=10.0, oi=50.0, delta=0.5)]
    profiles = compute_profile(only_call, ProfileVariant.COMBINED, spot=7600.0)
    row = profiles[0]
    assert row.put_vol_component == 0.0
    assert row.put_oi_component == 0.0
    assert row.net == pytest.approx(10.0 * 0.5 + 50.0 * 0.5)

    with pytest.raises(ValueError, match="Neplatná strana opce"):
        compute_profile(
            [ProfileInput(strike=7600.0, right="Q", volume=1.0, oi=1.0, delta=0.5)],
            ProfileVariant.VOL,
            spot=7600.0,
        )


def test_put_delta_sign_is_ignored_via_abs() -> None:
    # |Δ| váha: záporná put delta nesmí obracet znaménko složek
    inputs = [ProfileInput(strike=7600.0, right="P", volume=10.0, oi=0.0, delta=-0.5)]
    profiles = compute_profile(inputs, ProfileVariant.VOL, spot=7600.0)
    assert profiles[0].put_vol_component == pytest.approx(5.0)
    assert profiles[0].net == pytest.approx(-5.0)  # put táhne profil doleva (C − P)
