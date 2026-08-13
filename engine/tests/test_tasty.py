"""tasty/ moduly (#613 PR B): cache eventů, redakce tokenů, dispatch, mapa symbolů."""

import datetime as dt
import json
import logging

import pytest

from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.tasty.mock import feed_greeks, feed_quote, feed_summary, feed_trade
from gexlens_engine.tasty.provider import TastyChainCache, _number
from gexlens_engine.tasty.session import TastyCredentials, redact
from gexlens_engine.tasty.stream import EVENT_FIELDS, DxLinkStream
from gexlens_engine.tasty.symbols import ChainSymbols

TS = dt.datetime(2026, 8, 13, 14, 0, tzinfo=dt.UTC)


def test_cache_drzi_posledni_stav_per_symbol() -> None:
    cache = TastyChainCache(clock=lambda: TS)
    feed_quote(cache, "./E2DQ26C7775:XCME", 18.0, 18.5)
    feed_greeks(cache, "./E2DQ26C7775:XCME", 0.126, 0.52, 0.0112)
    feed_summary(cache, "./E2DQ26C7775:XCME", 989)
    feed_quote(cache, "./E2DQ26C7775:XCME", 18.25, 18.75)  # novější přepíše

    state = cache.state("./E2DQ26C7775:XCME")
    assert state is not None
    assert state.quote.bid == 18.25
    assert state.greeks.iv == 0.126
    assert state.summary.open_interest == 989
    assert state.quote.updated_at == TS
    assert cache.symbols_tracked() == 1


def test_pokryti_agresora_se_pocita() -> None:
    cache = TastyChainCache(clock=lambda: TS)
    feed_trade(cache, "X", "BUY")
    feed_trade(cache, "X", "SELL")
    feed_trade(cache, "X", None)  # UNDEFINED — bez strany
    state = cache.state("X")
    assert state is not None
    assert state.trades == 3
    assert state.trades_with_aggressor == 2


def test_nan_a_null_hodnoty_jsou_none() -> None:
    assert _number("NaN") is None
    assert _number(None) is None
    assert _number(float("nan")) is None
    assert _number("18.5") == 18.5
    assert _number(7.0) == 7.0


def test_redakce_tajemstvi_v_logu_a_repr(caplog: pytest.LogCaptureFixture) -> None:
    """#620: token se nesmí objevit v logu ani v repr (precedens #553)."""
    secret = "super-tajny-refresh-token-1234567890"
    credentials = TastyCredentials(client_secret=secret, refresh_token=secret)
    assert secret not in repr(credentials)
    assert "1234567890" not in repr(credentials)
    with caplog.at_level(logging.INFO):
        logging.getLogger("test").info("token obnoven (%s)", redact(secret))
    assert secret not in caplog.text


def test_dispatch_rozbali_compact_davku() -> None:
    """COMPACT dávka: víc záznamů téhož typu v jednom poli hodnot."""
    events: list[tuple[str, list[object]]] = []

    async def fake_token() -> tuple[str, str]:
        return "wss://mock.invalid", "token"

    stream = DxLinkStream(
        token_source=fake_token,
        on_event=lambda event_type, values: events.append((event_type, values)),
    )
    width = len(EVENT_FIELDS["Quote"])
    data: list[object] = ["Quote", ["A", 1.0, 2.0, 1, 1, "B", 3.0, 4.0, 2, 2]]
    stream._dispatch(data)
    assert len(events) == 2
    assert events[0][1][0] == "A"
    assert events[1][1][0] == "B"
    assert all(len(values) == width for _, values in events)


def test_chain_symbols_mapuje_nase_kontrakty() -> None:
    chain = ChainSymbols(
        product="ES",
        day=TS.date(),
        by_contract={
            ("20260813", 7775.0, "C"): "./E2DQ26C7775:XCME",
            ("20260813", 7775.0, "P"): "./E2DQ26P7775:XCME",
        },
    )
    spec = OptionContractSpec(
        symbol="ES",
        sec_type="FOP",
        expiry="20260813",
        strike=7775.0,
        right="C",
        exchange="CME",
        trading_class="E2D",
        multiplier="50",
    )
    assert chain.streamer_symbol(spec) == "./E2DQ26C7775:XCME"
    missing = OptionContractSpec(
        symbol="ES",
        sec_type="FOP",
        expiry="20260813",
        strike=9999.0,
        right="C",
        exchange="CME",
        trading_class="E2D",
        multiplier="50",
    )
    assert chain.streamer_symbol(missing) is None


def test_event_fields_odpovidaji_sondovanym() -> None:
    """Smlouva s FEED_SETUP: pořadí polí je fixní — změna = rozbité rozbalení."""
    assert EVENT_FIELDS["TimeAndSale"][4] == "aggressorSide"
    assert EVENT_FIELDS["Summary"][1] == "openInterest"
    assert json.dumps(EVENT_FIELDS)  # serializovatelné do FEED_SETUP
