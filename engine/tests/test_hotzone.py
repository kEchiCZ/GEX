"""Testy HotZoneCollectoru (issue #8): Lee–Ready klasifikace, degradace, rebalance."""

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.hotzone import (
    ClassifiedTrade,
    HotZoneCollector,
    HotZoneStatus,
    TradeSide,
    classify_lee_ready,
)
from gexlens_engine.ibkr.mock import MockHotZoneClient

SPOT = 7600.0


def chain(strike_lo: int = 7500, strike_hi: int = 7700) -> list[OptionContractSpec]:
    return [
        OptionContractSpec(
            symbol="ES",
            sec_type="FOP",
            expiry="20260716",
            strike=float(strike),
            right=right,
            exchange="CME",
            trading_class="E3D",
            multiplier="50",
        )
        for strike in range(strike_lo, strike_hi + 5, 5)
        for right in ("C", "P")
    ]


def spec_at(contracts: list[OptionContractSpec], strike: float, right: str) -> OptionContractSpec:
    return next(c for c in contracts if c.strike == strike and c.right == right)


# ── Lee–Ready klasifikátor ─────────────────────────────────────────


def test_lee_ready_at_or_beyond_quotes() -> None:
    assert classify_lee_ready(10.5, 10.0, 10.5, 0) is TradeSide.BUY  # na asku
    assert classify_lee_ready(10.7, 10.0, 10.5, 0) is TradeSide.BUY  # nad askem
    assert classify_lee_ready(10.0, 10.0, 10.5, 0) is TradeSide.SELL  # na bidu
    assert classify_lee_ready(9.8, 10.0, 10.5, 0) is TradeSide.SELL  # pod bidem


def test_lee_ready_between_quotes_vs_mid() -> None:
    assert classify_lee_ready(10.4, 10.0, 10.5, 0) is TradeSide.BUY  # nad midem
    assert classify_lee_ready(10.1, 10.0, 10.5, 0) is TradeSide.SELL  # pod midem


def test_lee_ready_exactly_on_mid_uses_tick_test() -> None:
    assert classify_lee_ready(10.25, 10.0, 10.5, last_tick_sign=1) is TradeSide.BUY
    assert classify_lee_ready(10.25, 10.0, 10.5, last_tick_sign=-1) is TradeSide.SELL
    assert classify_lee_ready(10.25, 10.0, 10.5, last_tick_sign=0) is TradeSide.UNKNOWN


# ── Klasifikace v kolektoru (AC: buy/sell/mid-tick-test) ───────────


async def test_collector_classifies_trades() -> None:
    contracts = chain()
    client = MockHotZoneClient()
    collector = HotZoneCollector(client, Settings(hot_zone_width=2, tick_by_tick_max_streams=100))
    trades: list[ClassifiedTrade] = []
    collector.on_trade_classified(trades.append)
    await collector.rebalance(contracts, SPOT)

    atm_call = spec_at(contracts, 7600.0, "C")
    collector.on_quote(atm_call, bid=10.0, ask=10.5)

    collector.on_trade(atm_call, price=10.5, size=2, ts=1.0)  # ask → BUY
    collector.on_trade(atm_call, price=10.0, size=1, ts=2.0)  # bid → SELL
    # Mid 10.25: poslední změna ceny 10.5→10.0 klesající → tick test → SELL
    collector.on_trade(atm_call, price=10.25, size=3, ts=3.0)
    # Mid po růstu 10.0→10.25 → tick test → BUY
    collector.on_trade(atm_call, price=10.25, size=1, ts=4.0)

    assert [t.side for t in trades] == [
        TradeSide.BUY,
        TradeSide.SELL,
        TradeSide.SELL,
        TradeSide.BUY,
    ]
    assert trades[0].size == 2
    assert trades[2].spec == atm_call


async def test_trade_without_quote_is_unknown() -> None:
    contracts = chain()
    client = MockHotZoneClient()
    collector = HotZoneCollector(client, Settings(hot_zone_width=1, tick_by_tick_max_streams=100))
    trades: list[ClassifiedTrade] = []
    collector.on_trade_classified(trades.append)
    await collector.rebalance(contracts, SPOT)

    collector.on_trade(spec_at(contracts, 7600.0, "C"), price=10.0, size=1, ts=1.0)
    assert trades[0].side is TradeSide.UNKNOWN


# ── Složení zóny a degradace ───────────────────────────────────────


async def test_zone_full_width_when_budget_allows() -> None:
    contracts = chain()
    client = MockHotZoneClient()
    settings = Settings(hot_zone_width=15, tick_by_tick_max_streams=100)
    collector = HotZoneCollector(client, settings)

    await collector.rebalance(contracts, SPOT)

    # ±15 strikes kolem 7600 v řetězci 7500–7700: dole 7525–7600, nahoře 7600–7675
    assert len(collector.active_contracts) == 62  # 31 strikes × C/P
    assert not collector.status.degraded


async def test_zone_degrades_to_stream_budget() -> None:
    contracts = chain()
    client = MockHotZoneClient()
    settings = Settings(hot_zone_width=15, tick_by_tick_max_streams=5)  # ADR-0001
    collector = HotZoneCollector(client, settings)

    await collector.rebalance(contracts, SPOT)

    # Budget 5: ATM pár (2) + nejbližší strike pár (4); třetí pár by přetekl → konec
    assert len(collector.active_contracts) == 4
    status = collector.status
    assert status.degraded
    assert status.active_streams == 4
    assert status.stream_budget == 5
    atm_strikes = {c.strike for c in collector.active_contracts}
    assert atm_strikes == {7600.0, 7595.0} or atm_strikes == {7600.0, 7605.0}


async def test_runtime_stream_limit_degrades_budget() -> None:
    contracts = chain()
    client = MockHotZoneClient(stream_limit=3)  # účet reálně povolí jen 3
    settings = Settings(hot_zone_width=15, tick_by_tick_max_streams=10)
    collector = HotZoneCollector(client, settings)
    statuses: list[HotZoneStatus] = []
    collector.on_status(statuses.append)

    await collector.rebalance(contracts, SPOT)

    assert len(client.active) == 3
    assert collector.status.stream_budget == 3  # degradováno na realitu
    assert statuses[-1].degraded


# ── Rebalance při pohybu spotu (AC: bez ztráty běžících streamů) ───


async def test_rebalance_keeps_streams_inside_zone() -> None:
    contracts = chain()
    client = MockHotZoneClient()
    settings = Settings(hot_zone_width=2, tick_by_tick_max_streams=100)
    collector = HotZoneCollector(client, settings)

    await collector.rebalance(contracts, 7600.0)
    initial = collector.active_contracts
    assert {c.strike for c in initial} == {7590.0, 7595.0, 7600.0, 7605.0, 7610.0}
    subscribe_count_before = len(client.subscribe_calls)

    await collector.rebalance(contracts, 7605.0)  # posun o 1 strike krok

    current = collector.active_contracts
    assert {c.strike for c in current} == {7595.0, 7600.0, 7605.0, 7610.0, 7615.0}
    # Odhlášen jen okraj (7590 C/P), nové subskripce jen 7615 C/P
    assert {c.strike for c in client.unsubscribe_calls} == {7590.0}
    assert len(client.subscribe_calls) == subscribe_count_before + 2
    # Kontrakty uvnitř zóny si stream ponechaly
    assert initial & current == {c for c in initial if c.strike != 7590.0}


async def test_small_spot_move_does_not_rebalance() -> None:
    contracts = chain()
    client = MockHotZoneClient()
    settings = Settings(hot_zone_width=2, tick_by_tick_max_streams=100)
    collector = HotZoneCollector(client, settings)

    await collector.rebalance(contracts, 7600.0)
    calls_before = len(client.subscribe_calls)

    await collector.rebalance(contracts, 7602.0)  # < 1 strike krok (5)

    assert len(client.subscribe_calls) == calls_before
    assert not client.unsubscribe_calls
