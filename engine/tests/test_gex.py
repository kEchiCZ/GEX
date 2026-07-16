"""Testy GEX enginu (issue #13): golden dataset, výměna znaménkového modelu, edge cases."""

import json
from pathlib import Path

import pytest

from gexlens_engine.compute.gex import GexEngine, GexInput, GexResult

GOLDEN_PATH = Path(__file__).parent / "golden" / "gex_basic.json"


def load_golden() -> tuple[list[GexInput], float, float, dict[str, object]]:
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    options = [GexInput(**option) for option in data["options"]]
    return options, data["spot"], data["multiplier"], data["expected"]


def compute_golden() -> tuple[GexResult, dict[str, object]]:
    options, spot, multiplier, expected = load_golden()
    return GexEngine().compute(options, spot=spot, multiplier=multiplier), expected


def test_golden_per_strike_values() -> None:
    result, expected = compute_golden()
    expected_strikes = expected["per_strike"]
    assert isinstance(expected_strikes, dict)

    assert len(result.per_strike) == len(expected_strikes)
    for row in result.per_strike:
        golden = expected_strikes[str(row.strike)]
        assert isinstance(golden, dict)
        assert row.call_gex_1pt == pytest.approx(golden["call_gex_1pt"])
        assert row.put_gex_1pt == pytest.approx(golden["put_gex_1pt"])
        assert row.net_gex_1pt == pytest.approx(golden["net_gex_1pt"])
        assert row.net_gex_1pct == pytest.approx(golden["net_gex_1pct"])


def test_golden_totals() -> None:
    result, expected = compute_golden()
    assert result.total_gex_1pt == pytest.approx(expected["total_gex_1pt"])
    assert result.total_gex_1pct == pytest.approx(expected["total_gex_1pct"])


def test_net_by_strike_matches_per_strike() -> None:
    result, _ = compute_golden()
    net = result.net_by_strike()
    assert net[7590.0] == pytest.approx(-470.0)
    assert list(net) == sorted(net)  # seřazeno podle striku


def test_sign_model_swap_keeps_api() -> None:
    """AC: výměna strategie nemění API — jen znaménka příspěvků."""

    class AbsoluteModel:
        """Ukázková alternativní strategie: obě strany kladně (|GEX| profil)."""

        def sign(self, option: GexInput) -> float:
            return 1.0

    options, spot, multiplier, _ = load_golden()
    result = GexEngine(sign_model=AbsoluteModel()).compute(
        options, spot=spot, multiplier=multiplier
    )

    # NetGEX = GEX_C + GEX_P (místo rozdílu); volání i návratový typ beze změny
    row = next(r for r in result.per_strike if r.strike == 7590.0)
    assert row.net_gex_1pt == pytest.approx(480.0 + 950.0)
    assert row.call_gex_1pt == pytest.approx(480.0)  # složky stran se modelem nemění
    assert result.total_gex_1pt == pytest.approx(sum(r.net_gex_1pt for r in result.per_strike))


def test_empty_chain_gives_zero_totals() -> None:
    result = GexEngine().compute([], spot=7600.0, multiplier=50.0)
    assert result.per_strike == ()
    assert result.total_gex_1pt == 0.0
    assert result.total_gex_1pct == 0.0


def test_invalid_right_rejected() -> None:
    with pytest.raises(ValueError, match="Neplatná strana opce"):
        GexEngine().compute(
            [GexInput(strike=7600.0, right="X", gamma=0.01, oi=100.0)],
            spot=7600.0,
            multiplier=50.0,
        )


def test_one_sided_strike_fills_missing_side_with_zero() -> None:
    result = GexEngine().compute(
        [GexInput(strike=7600.0, right="C", gamma=0.01, oi=100.0)],
        spot=7600.0,
        multiplier=50.0,
    )
    row = result.per_strike[0]
    assert row.call_gex_1pt == pytest.approx(50.0)
    assert row.put_gex_1pt == 0.0
    assert row.net_gex_1pt == pytest.approx(50.0)
