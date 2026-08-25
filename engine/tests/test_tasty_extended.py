"""Testy extended expirací (#616 4a): plán, disjunktnost, kadence, konsolidace."""

import datetime as dt

import pytest

from gexlens_engine.tasty.extended import (
    ExpiryOverlapError,
    build_snapshot_rows,
    cadence_due,
    plan_extended_expiries,
    validate_disjoint,
)
from gexlens_engine.tasty.provider import TastyChainCache
from gexlens_engine.tasty.symbols import ChainSymbols

TODAY = dt.date(2026, 8, 22)
NOW = dt.datetime(2026, 8, 22, 14, 30, tzinfo=dt.UTC)


def chain_with(expiries: list[str]) -> ChainSymbols:
    by_contract = {(expiry, 6400.0, "C"): f"./E{i}C6400:XCME" for i, expiry in enumerate(expiries)}
    return ChainSymbols(product="ES", day=TODAY, by_contract=by_contract)


def test_plan_vynechava_ibkr_prosle_a_za_horizontem() -> None:
    chain = chain_with(["20260821", "20260824", "20260825", "20260918", "20260930"])

    planned = plan_extended_expiries(chain, {"20260824"}, today=TODAY, horizon_days=30)

    # 0821 prošlá, 0824 drží IBKR, 0930 za horizontem (30 dnů = do 21. 9.)
    assert planned == ["20260825", "20260918"]


def test_plan_bez_ibkr_pokryva_vse_v_horizontu() -> None:
    """Start bez TWS (#756): extended = kompletní množina (ADR-0025 dodatek)."""
    chain = chain_with(["20260824", "20260825"])

    assert plan_extended_expiries(chain, set(), today=TODAY, horizon_days=30) == [
        "20260824",
        "20260825",
    ]


def test_validate_disjoint_prekryv_je_chyba() -> None:
    with pytest.raises(ExpiryOverlapError):
        validate_disjoint(["20260824", "20260825"], {"20260825"})
    validate_disjoint(["20260824"], {"20260825"})  # disjunktní projde


def test_cadence_odstupnovana() -> None:
    def due(expiry: str, minute_of_day: int) -> bool:
        return cadence_due(
            expiry, today=TODAY, minute_of_day=minute_of_day, near_days=7, far_interval_min=5
        )

    # Blízká expirace: každou minutu; vzdálená jen každou pátou
    assert due("20260825", 871) is True
    assert due("20260930", 871) is False
    assert due("20260930", 870) is True


def test_build_rows_bs_greeks_a_oimissing() -> None:
    """Čerstvá kotace bez dxFeed Greeks → BS dopočet; OI chybí → oimissing."""
    expiry = "20260930"
    streamer = "./ESU6C6400:XCME"
    chain = ChainSymbols(product="ES", day=TODAY, by_contract={(expiry, 6400.0, "C"): streamer})
    cache = TastyChainCache(clock=lambda: NOW)
    cache.on_event("Quote", [streamer, 120.0, 122.0, 5, 5])

    rows, oi_missing = build_snapshot_rows(
        chain, expiry, cache, ts_min=NOW, spot=6450.0, now_utc=NOW, max_age_s=120.0
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.bid == 120.0 and row.ask == 122.0
    assert row.volume is None  # flows jsou výhradně IBKR
    assert row.iv is not None and 0.05 < row.iv < 1.0  # BS inverze z mid
    assert row.delta is not None and 0.4 < row.delta < 0.9  # ITM call
    assert row.oi == 0.0
    assert len(oi_missing) == 1  # bez Summary → poctivě oimissing (#465)


def test_build_rows_stara_kotace_se_preskoci() -> None:
    expiry = "20260930"
    streamer = "./ESU6C6400:XCME"
    chain = ChainSymbols(product="ES", day=TODAY, by_contract={(expiry, 6400.0, "C"): streamer})
    stale_time = NOW - dt.timedelta(minutes=10)
    cache = TastyChainCache(clock=lambda: stale_time)
    cache.on_event("Quote", [streamer, 120.0, 122.0, 5, 5])

    rows, oi_missing = build_snapshot_rows(
        chain, expiry, cache, ts_min=NOW, spot=6450.0, now_utc=NOW, max_age_s=120.0
    )

    assert rows == [] and oi_missing == []


def test_extended_streamers_pasmo_a_fallback_centra() -> None:
    """#616 kapacita: subskribuje se jen ±band kolem centra, ne plný chain."""
    from gexlens_engine.tasty.extended import extended_streamers

    by_contract = {
        ("20260826", float(strike), "C"): f".ES{strike}C" for strike in range(6000, 7001, 100)
    }
    chain = ChainSymbols(product="ES", day=TODAY, by_contract=by_contract)

    # Se spotem: jen striky v pásmu (±3,1 % z 6500 = ±201,5 b)
    subset = extended_streamers(chain, ["20260826"], center=6500.0, band_pct=3.1)
    assert subset == {".ES6300C", ".ES6400C", ".ES6500C", ".ES6600C", ".ES6700C"}

    # Bez spotu: centrum = medián striků nejbližší expirace (6500), ±1,6 % = ±104 b
    fallback = extended_streamers(chain, ["20260826"], center=None, band_pct=1.6)
    assert fallback == {".ES6400C", ".ES6500C", ".ES6600C"}

    # Neplánovaná expirace = nic
    assert extended_streamers(chain, [], center=6500.0, band_pct=3.1) == set()

    # Procentní pásmo škáluje s cenou podkladu (NQ ~4× ES; absolutní body by
    # křídla NQ ořezaly na čtvrtinu relativního pokrytí — lekce ADR-0004)
    nq_contracts = {
        ("20260826", float(strike), "C"): f".NQ{strike}C" for strike in range(24000, 28001, 400)
    }
    nq_chain = ChainSymbols(product="NQ", day=TODAY, by_contract=nq_contracts)
    nq = extended_streamers(nq_chain, ["20260826"], center=26000.0, band_pct=3.1)
    assert nq == {".NQ25200C", ".NQ25600C", ".NQ26000C", ".NQ26400C", ".NQ26800C"}


def test_odstupnovane_pasmo_siroke_pro_blizke_expirace() -> None:
    """#828 A: kapacita je konečná, tak se dá tam, kde je masa OTM putů."""
    from gexlens_engine.tasty.extended import extended_streamers

    by_contract = {}
    for expiry in ("20260825", "20260915"):
        for strike in range(6000, 7001, 100):
            by_contract[(expiry, float(strike), "C")] = f".ES{expiry}{strike}C"
    chain = ChainSymbols(product="ES", day=TODAY, by_contract=by_contract)
    planned = ["20260825", "20260915"]

    out = extended_streamers(
        chain,
        planned,
        center=6500.0,
        band_pct=3.1,  # ±201 b pro vzdálené
        near_band_pct=8.0,  # ±520 b pro nejbližší
        near_expiries=frozenset({"20260825"}),
    )

    near = sorted(s for s in out if "20260825" in s)
    far = sorted(s for s in out if "20260915" in s)
    # Blízká expirace sahá hlouběji do křídel než vzdálená
    assert len(near) > len(far)
    assert ".ES202608256000C" in near  # −500 b, uvnitř ±8 %
    assert ".ES202609156000C" not in far  # tentýž strike u vzdálené už ne

    # Bez near_band_pct se chová jako dřív (jedno pásmo pro všechny)
    uniform = extended_streamers(chain, planned, center=6500.0, band_pct=3.1)
    assert len([s for s in uniform if "20260825" in s]) == len(far)
