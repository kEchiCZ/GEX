"""Testy sdíleného FA odhadu OI (compute/flowoi, #232, ADR-0011 fáze 2)."""

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
