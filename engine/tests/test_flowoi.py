"""Testy sdíleného FA odhadu OI (compute/flowoi, #232, ADR-0011 fáze 2)."""

import pytest

from gexlens_engine.compute.flowoi import oi_estimate


def test_odhad_pricita_alfa_nasobek_netu() -> None:
    assert oi_estimate(1000.0, 100.0, 0.4) == 1040.0
    assert oi_estimate(1000.0, -100.0, 0.4) == 960.0


def test_podlaha_nula() -> None:
    """Pozice nemůže být záporná — net prodej větší než ranní OI končí na nule."""
    assert oi_estimate(10.0, -1000.0, 0.4) == 0.0


def test_alpha_nula_vraci_mereni() -> None:
    assert oi_estimate(1000.0, 500.0, 0.0) == 1000.0


def test_nulovy_net_vraci_mereni() -> None:
    assert oi_estimate(1000.0, 0.0, 0.4) == 1000.0


def test_v6_objem_seance_odhad_jen_zveda() -> None:
    """ADR-0061: pod v6 vstupuje objem strany za seanci (≥ 0) a ρ ≥ 0 — odhad OI jen zvedá.

    NQ 14. 10. 2026, call 30 300: OI 0, net −77, objem 231 — α·net dal 0, ρ 0,34·231 ≈ 79.
    """
    assert oi_estimate(0.0, 231.0, 0.34) == pytest.approx(78.54)
    assert oi_estimate(1000.0, 200.0, 0.25) == 1050.0
