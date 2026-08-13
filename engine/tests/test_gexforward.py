"""Forward GEX (#519): útes po expiraci, IV fallback, obchodní dny."""

import datetime as dt
import json
import math
from pathlib import Path

from gexlens_engine.compute.gexforward import (
    ForwardContract,
    day_reference_ts,
    fill_iv_by_moneyness,
    forward_field,
    trading_days_until_friday,
)
from gexlens_engine.compute.settle import settle_ts

GOLDEN = Path(__file__).parent / "golden" / "forward_gex_519.json"

# Středa — 0DTE dnes, weekly v pátek; do konce týdne 3 obchodní dny
TODAY = dt.date(2026, 8, 12)
EXPIRY_0DTE = "20260812"
EXPIRY_FRI = "20260814"

CHAIN = [
    ForwardContract(expiry=EXPIRY_0DTE, strike=7500.0, right="C", oi=1000.0, iv=0.20),
    ForwardContract(expiry=EXPIRY_0DTE, strike=7500.0, right="P", oi=400.0, iv=0.21),
    ForwardContract(expiry=EXPIRY_FRI, strike=7500.0, right="C", oi=300.0, iv=0.22),
    ForwardContract(expiry=EXPIRY_FRI, strike=7600.0, right="P", oi=500.0, iv=0.23),
]

GRID = {"grid_start": 7400.0, "grid_stop": 7700.0, "grid_step": 50.0}
MULTIPLIER = 50.0


def scalar_net(contracts: list[ForwardContract], ref: dt.datetime, spot: float) -> float:
    """Nezávislá skalární reference — stejný BS vzorec, žádné numpy."""
    tau_floor = 300.0
    year_s = 365.0 * 24 * 3600
    net = 0.0
    for c in contracts:
        settle = settle_ts(dt.datetime.strptime(c.expiry, "%Y%m%d").date())
        if settle <= ref:
            continue
        tau = max((settle - ref).total_seconds(), tau_floor) / year_s
        assert c.iv is not None
        sqrt_tau = math.sqrt(tau)
        d1 = (math.log(spot / c.strike) + 0.5 * c.iv * c.iv * tau) / (c.iv * sqrt_tau)
        gamma = math.exp(-0.5 * d1 * d1) / (math.sqrt(2 * math.pi) * spot * c.iv * sqrt_tau)
        net += (1.0 if c.right == "C" else -1.0) * gamma * c.oi
    return net * MULTIPLIER


def test_trading_days_preskakuji_vikend() -> None:
    assert trading_days_until_friday(dt.date(2026, 8, 12)) == [
        dt.date(2026, 8, 12),
        dt.date(2026, 8, 13),
        dt.date(2026, 8, 14),
    ]
    # Pátek = jen pátek; sobota = prázdno
    assert trading_days_until_friday(dt.date(2026, 8, 14)) == [dt.date(2026, 8, 14)]
    assert trading_days_until_friday(dt.date(2026, 8, 15)) == []


def test_utes_po_0dte_odpovida_rucnimu_zbytku() -> None:
    """AC #519: pole dne po expiraci == pole spočtené z OI BEZ té expirace."""
    field = forward_field(CHAIN, today=TODAY, multiplier=MULTIPLIER, **GRID)
    assert field is not None
    assert [block.date.isoformat() for block in field.days] == [
        "2026-08-12",
        "2026-08-13",
        "2026-08-14",
    ]
    day1, day2, _day3 = field.days

    # Den 1: 0DTE ještě žije (reference = poledne CT, settle 16:00 ET)
    assert day1.dropped_expiries == ()
    assert day1.dropped_share is None
    # Den 2: 0DTE odpadla — hodnoty odpovídají nezávislé skalární referenci
    assert day2.dropped_expiries == (EXPIRY_0DTE,)
    remainder = [c for c in CHAIN if c.expiry != EXPIRY_0DTE]
    ref2 = day_reference_ts(dt.date(2026, 8, 13))
    for index, value in enumerate(day2.values):
        spot = GRID["grid_start"] + index * GRID["grid_step"]
        assert abs(value - scalar_net(remainder, ref2, spot)) < 0.05, f"bod {spot}"
    # Podíl odpadlé gammy: 0DTE dominuje (1400 kontraktů ATM proti 800)
    assert day2.dropped_share is not None
    assert 0.5 < day2.dropped_share < 1.0

    # Den 1 pro kontrolu taky sedí na referenci (všechny kontrakty)
    ref1 = day_reference_ts(TODAY)
    for index, value in enumerate(day1.values):
        spot = GRID["grid_start"] + index * GRID["grid_step"]
        assert abs(value - scalar_net(CHAIN, ref1, spot)) < 0.05


def test_golden_fixture_forward_pole() -> None:
    """Golden dataset (pravidlo 3): hodnoty zafixované při implementaci #519."""
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    field = forward_field(CHAIN, today=TODAY, multiplier=MULTIPLIER, **GRID)
    assert field is not None
    for block, gold in zip(field.days, expected["days"], strict=True):
        assert block.date.isoformat() == gold["day"]
        assert list(block.dropped_expiries) == gold["dropped_expiries"]
        for value, gold_value in zip(block.values, gold["values"], strict=True):
            assert abs(value - gold_value) < 0.05


def test_iv_fallback_z_nejblizsi_expirace() -> None:
    contracts = [
        ForwardContract(expiry=EXPIRY_0DTE, strike=7500.0, right="C", oi=10.0, iv=0.20),
        ForwardContract(expiry=EXPIRY_0DTE, strike=7550.0, right="C", oi=10.0, iv=0.24),
        # Páteční bez IV → vezme 0DTE hodnotu nejbližšího striku
        ForwardContract(expiry=EXPIRY_FRI, strike=7560.0, right="C", oi=10.0, iv=None),
        # Put bez IV a bez jediné měřené put řady → vypadne
        ForwardContract(expiry=EXPIRY_FRI, strike=7500.0, right="P", oi=10.0, iv=None),
    ]
    filled, share = fill_iv_by_moneyness(contracts)
    assert len(filled) == 3
    fallback = next(c for c in filled if c.expiry == EXPIRY_FRI)
    assert fallback.iv == 0.24  # nejbližší strike 7550 měřené expirace
    assert share == 1 / 3


def test_bez_iv_a_oi_vraci_none() -> None:
    empty = [ForwardContract(expiry=EXPIRY_FRI, strike=7500.0, right="C", oi=0.0, iv=None)]
    assert forward_field(empty, today=TODAY, multiplier=MULTIPLIER, **GRID) is None
