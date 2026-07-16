"""Testy ChainDiscovery (issue #6) nad MockIB: expirace, pásmo strikes, auto-rozšíření."""

import pytest
from pydantic import ValidationError

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import (
    ChainDiscovery,
    ExpiryInfo,
    Underlying,
    build_contracts,
    option_sec_type,
    select_band,
    should_expand,
)
from gexlens_engine.ibkr.mock import MockIB, MockOptionChain

ES_STRIKES = [float(k) for k in range(7000, 8205, 5)]

ES = Underlying(symbol="ES", sec_type="FUT", exchange="CME", con_id=1)
SPY = Underlying(symbol="SPY", sec_type="STK", exchange="SMART", con_id=2)


def es_chains() -> list[MockOptionChain]:
    return [
        MockOptionChain("CME", "EW3", "50", ["20260717"], ES_STRIKES),
        MockOptionChain("CME", "E3D", "50", ["20260716"], ES_STRIKES),
        MockOptionChain("CME", "ES", "50", ["20260918", "20261218"], ES_STRIKES),
        # Jiná burza — musí být odfiltrována
        MockOptionChain("QBALGO", "E3D", "50", ["20260716"], ES_STRIKES),
    ]


async def test_discover_es_returns_sorted_trading_classes() -> None:
    client = MockIB(option_chains=es_chains())
    discovery = ChainDiscovery(client, Settings())

    infos = await discovery.discover(ES)

    assert [(i.trading_class, i.expiry) for i in infos] == [
        ("E3D", "20260716"),
        ("EW3", "20260717"),
        ("ES", "20260918"),
        ("ES", "20261218"),
    ]
    assert all(i.exchange == "CME" for i in infos)
    assert client.sec_def_requests == [("ES", "CME", "FUT", 1)]


async def test_discover_stock_filters_smart_and_empty_fop_exchange() -> None:
    chains = [
        MockOptionChain("SMART", "SPY", "100", ["20260717"], [400.0, 405.0]),
        MockOptionChain("CBOE", "SPY", "100", ["20260717"], [400.0, 405.0]),
    ]
    client = MockIB(option_chains=chains)
    discovery = ChainDiscovery(client, Settings())

    infos = await discovery.discover(SPY)

    assert len(infos) == 1
    assert infos[0].exchange == "SMART"
    # OPT discovery posílá prázdný futFopExchange (SPEC 3.2)
    assert client.sec_def_requests == [("SPY", "", "STK", 2)]


def test_select_band_takes_strikes_within_range() -> None:
    band = select_band(ES_STRIKES, spot=7600.0, range_points=200.0)
    assert band.strikes[0] == 7400.0
    assert band.strikes[-1] == 7800.0
    assert len(band.strikes) == 81  # krok 5 → 81 strikes v ±200
    assert band.low == 7400.0
    assert band.high == 7800.0


def test_build_contracts_complete_band() -> None:
    band = select_band(ES_STRIKES, spot=7600.0, range_points=200.0)
    info = ExpiryInfo("E3D", "20260716", "CME", "50", tuple(ES_STRIKES))

    contracts = build_contracts(ES, info, band)

    assert len(contracts) == 2 * len(band.strikes)  # C + P pro každý strike
    assert {c.right for c in contracts} == {"C", "P"}
    assert all(c.sec_type == "FOP" for c in contracts)
    assert all(c.trading_class == "E3D" for c in contracts)


def test_option_sec_type_mapping_and_rejection() -> None:
    assert option_sec_type("FUT") == "FOP"
    assert option_sec_type("STK") == "OPT"
    assert option_sec_type("IND") == "OPT"
    with pytest.raises(ValueError, match="Nepodporovaný secType"):
        option_sec_type("BOND")


def test_should_expand_near_edge_only() -> None:
    band = select_band(ES_STRIKES, spot=7600.0, range_points=200.0)
    # Spot uprostřed pásma → nerozšiřovat
    assert not should_expand(band, spot=7600.0, threshold=0.25)
    # Spot 30 b od horního okraje (< 25 % ze šířky 400) → rozšířit
    assert should_expand(band, spot=7770.0, threshold=0.25)
    # Symetricky u dolního okraje
    assert should_expand(band, spot=7430.0, threshold=0.25)


async def test_maybe_expand_grows_near_edge_and_keeps_wings() -> None:
    """ADR-0002 v2: prodlužuje se jen aktivní okraj — křídla se neztrácejí."""
    client = MockIB(option_chains=es_chains())
    discovery = ChainDiscovery(client, Settings())
    info = ExpiryInfo("E3D", "20260716", "CME", "50", tuple(ES_STRIKES))

    band = discovery.initial_band(info, spot=7600.0)  # obálka 7400–7800
    unchanged = discovery.maybe_expand(info, band, spot=7600.0)
    assert unchanged.expanded is False
    assert unchanged.band == band

    result = discovery.maybe_expand(info, band, spot=7770.0)  # blízko horního okraje
    assert result.expanded is True
    assert result.capped is False
    assert result.band.high == 7970.0  # spot + 200
    assert result.band.low == 7400.0  # dolní křídlo zůstává!
    assert not should_expand(result.band, spot=7770.0, threshold=0.25)


async def test_maybe_expand_lower_edge_keeps_upper_wing() -> None:
    client = MockIB(option_chains=es_chains())
    discovery = ChainDiscovery(client, Settings())
    info = ExpiryInfo("E3D", "20260716", "CME", "50", tuple(ES_STRIKES))

    band = discovery.initial_band(info, spot=7600.0)
    result = discovery.maybe_expand(info, band, spot=7430.0)

    assert result.band.low == 7230.0  # spot − 200
    assert result.band.high == 7800.0  # horní křídlo zůstává


async def test_maybe_expand_caps_width_and_slides_active_edge() -> None:
    """Strop šířky: obálka se posouvá za spotem, vzdálený okraj se obětuje (capped)."""
    client = MockIB(option_chains=es_chains())
    settings = Settings(strike_range_points=200.0, strike_range_max_points=500.0)
    discovery = ChainDiscovery(client, settings)
    info = ExpiryInfo("E3D", "20260716", "CME", "50", tuple(ES_STRIKES))

    band = discovery.initial_band(info, spot=7600.0)  # 7400–7800 (šířka 400)
    first = discovery.maybe_expand(info, band, spot=7770.0)  # → 7400–7970 (šířka 570 > 500)

    assert first.expanded is True
    assert first.capped is True
    assert first.band.high == 7970.0  # aktivní okraj sleduje spot
    assert first.band.low == 7470.0  # dolní okraj dotažen na strop šířky
    assert first.band.width == 500.0


def test_settings_reject_max_width_below_default_envelope() -> None:
    with pytest.raises(ValidationError, match="strike_range_max_points"):
        Settings(strike_range_points=200.0, strike_range_max_points=300.0)
