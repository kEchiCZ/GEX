"""Stínové CumΔ z TimeAndSale (#615 fáze 3): zóny a pokrytí strany."""

import datetime as dt
from pathlib import Path

import pytest

from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.tasty.cumdelta_dx import DxCumDeltaShadow

TS = dt.datetime(2026, 8, 27, 14, 0, tzinfo=dt.UTC)


def spec(strike: float, right: str = "C") -> OptionContractSpec:
    return OptionContractSpec(
        symbol="ES",
        sec_type="FOP",
        expiry="20260827",
        strike=strike,
        right=right,
        exchange="CME",
        trading_class="E4D",
        multiplier="50",
    )


def make_shadow(spot: float = 7600.0) -> DxCumDeltaShadow:
    shadow = DxCumDeltaShadow(multiplier=50.0)
    # Žebřík 7500–7700 à 5 b: ATM 7600, hot ±1 strike, prstenec ±15
    universe = {f".ES{int(strike)}C": spec(strike) for strike in range(7500, 7705, 5)}
    shadow.set_universe(universe)
    shadow.set_spot(spot)
    shadow.roll_session(TS.date())
    return shadow


def test_cely_retez_je_jedna_rada_bez_zon() -> None:
    """ADR-0032 doplněk: každý strike univerza se počítá do hlavní řady, hot je 0."""
    shadow = make_shadow()
    shadow.on_trade(".ES7600C", size=2, aggressor="BUY", delta=0.5)  # ATM
    shadow.on_trade(".ES7610C", size=3, aggressor="BUY", delta=0.4)
    shadow.on_trade(".ES7680C", size=9, aggressor="BUY", delta=0.1)  # 16 striků od ATM
    row = shadow.close_minute(TS)
    assert row.flow_hot == 0.0
    assert row.flow_ring == pytest.approx((2 * 0.5 + 3 * 0.4 + 9 * 0.1) * 50.0)
    assert row.trades == 3
    # symbol mimo univerzum se nepočítá vůbec
    shadow.on_trade(".ES7800C", size=1, aggressor="SELL", delta=0.2)
    assert shadow.close_minute(TS).trades == 0


def test_vsechny_trady_prstence_jdou_do_jedne_rady() -> None:
    """Spread legy se nerozlišují (3. 9. 2026): CME příznak nenese, řada je jedna."""
    shadow = make_shadow()
    shadow.on_trade(".ES7610C", size=4, aggressor="BUY", delta=0.5)
    shadow.on_trade(".ES7615C", size=6, aggressor="SELL", delta=0.5)
    row = shadow.close_minute(TS)
    assert row.flow_ring == (4 - 6) * 0.5 * 50.0
    assert row.trades == 2
    assert row.volume == 10


def test_neznama_strana_se_nepocita_do_toku_ale_meri_se() -> None:
    """Bez aggressorSide žádný tok — pokrytí rozhoduje o midpoint fallbacku."""
    shadow = make_shadow()
    shadow.on_trade(".ES7610C", size=5, aggressor=None, delta=0.5)
    shadow.on_trade(".ES7610C", size=5, aggressor="UNDEFINED", delta=0.5)
    row = shadow.close_minute(TS)
    assert row.flow_ring == 0.0
    assert row.unknown_side == 2
    assert row.trades == 2 and row.volume == 10


def test_bez_spotu_nebo_delty_se_trade_zahazuje_a_pocita() -> None:
    """Díra v měření se přizná (dropped_no_context), tok se nevymýšlí.

    Bez spotu se od doplňku ADR-0032 počítá dál — zóny nejsou; bez delty ne."""
    shadow = make_shadow()
    shadow.set_spot(None)
    shadow.on_trade(".ES7610C", size=1, aggressor="BUY", delta=0.5)
    shadow.set_spot(7600.0)
    shadow.on_trade(".ES7610C", size=1, aggressor="BUY", delta=None)
    row = shadow.close_minute(TS)
    assert row.flow_ring == pytest.approx(1 * 0.5 * 50.0)
    assert row.dropped_no_context == 1


def test_kumulativy_a_roll_session() -> None:
    shadow = make_shadow()
    shadow.on_trade(".ES7610C", size=2, aggressor="BUY", delta=0.5)
    shadow.close_minute(TS)
    shadow.on_trade(".ES7610C", size=2, aggressor="BUY", delta=0.5)
    row = shadow.close_minute(TS + dt.timedelta(minutes=1))
    assert row.cum_ring == 2 * (2 * 0.5 * 50.0)
    # Nový den = reset (stejně jako živé CumΔ)
    assert shadow.roll_session(TS.date() + dt.timedelta(days=1))
    assert shadow.close_minute(TS + dt.timedelta(days=1)).cum_ring == 0.0
    # Týž den podruhé nic neresetuje
    assert not shadow.roll_session(TS.date() + dt.timedelta(days=1))


def test_write_dx_flow_roundtrip(tmp_path: Path) -> None:
    """Minutová řada se ukládá do derived/{sym}/cumdelta_dx a čte zpět 1:1."""
    import pyarrow.parquet as pq

    from gexlens_engine.config import Settings
    from gexlens_engine.storage.parquet_store import SnapshotWriter

    settings = Settings(data_dir=tmp_path)
    writer = SnapshotWriter(settings)
    shadow = make_shadow()
    shadow.on_trade(".ES7610C", size=4, aggressor="BUY", delta=0.5)
    row = shadow.close_minute(TS)

    path = writer.write_dx_flow("ES", TS.date(), [row])

    assert path == settings.derived_dir / "ES" / "cumdelta_dx" / "2026-08-27.parquet"
    record = pq.read_table(path).to_pylist()[0]
    assert record["flow_ring"] == 4 * 0.5 * 50.0
    assert record["volume"] == 4.0
    assert "spread_volume" not in record and "cum_ring_outright" not in record
    assert record["ts_min"] == TS
