"""Testy podkladových dat (issue #10): 1min agregace, PacingGuard, backfill bez violation."""

import asyncio
import datetime as dt
import time

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.mock import MockHistoricalClient
from gexlens_engine.ibkr.pacing import PacingGuard
from gexlens_engine.ibkr.underlying import Bar, RealTimeBarAggregator, UnderlyingBackfiller

TODAY = dt.date(2026, 7, 16)


def bar_5s(minute: int, second: int, close: float, volume: float = 1.0) -> Bar:
    ts = dt.datetime(2026, 7, 16, 15, minute, second, tzinfo=dt.UTC)
    return Bar(
        ts=ts, open=close - 0.25, high=close + 0.5, low=close - 0.5, close=close, volume=volume
    )


# ── RealTimeBarAggregator ──────────────────────────────────────────


def test_aggregates_twelve_5s_bars_into_one_minute() -> None:
    minutes: list[Bar] = []
    aggregator = RealTimeBarAggregator(minutes.append)

    for second in range(0, 60, 5):
        aggregator.add_5s_bar(bar_5s(0, second, close=100.0 + second))
    aggregator.add_5s_bar(bar_5s(1, 0, close=200.0))  # překročení hranice minuty

    assert len(minutes) == 1
    minute = minutes[0]
    assert minute.ts == dt.datetime(2026, 7, 16, 15, 0, tzinfo=dt.UTC)
    assert minute.open == 100.0 - 0.25  # open prvního 5s baru
    assert minute.close == 155.0  # close posledního 5s baru minuty
    assert minute.high == 155.5
    assert minute.low == 99.5
    assert minute.volume == 12.0


def test_flush_emits_partial_minute() -> None:
    minutes: list[Bar] = []
    aggregator = RealTimeBarAggregator(minutes.append)
    aggregator.add_5s_bar(bar_5s(0, 0, close=100.0))
    aggregator.add_5s_bar(bar_5s(0, 5, close=101.0))

    flushed = aggregator.flush()

    assert len(minutes) == 1
    assert flushed is not None
    assert flushed.close == 101.0
    assert aggregator.flush() is None  # druhý flush nemá co emitovat


# ── PacingGuard ────────────────────────────────────────────────────


async def test_guard_prevents_pacing_violation_under_burst() -> None:
    client = MockHistoricalClient(max_requests=3, window_s=0.2)
    guard = PacingGuard(max_requests=3, window_s=0.2)

    async def one(day_offset: int) -> None:
        await guard.run(
            key=("ES", day_offset),
            func=lambda: client.fetch_day_bars("ES", TODAY - dt.timedelta(days=day_offset)),
        )

    start = time.monotonic()
    await asyncio.gather(*(one(i) for i in range(10)))  # bez guardu by 4. request spadl
    duration = time.monotonic() - start

    assert len(client.calls) == 10
    assert duration >= 0.4  # 10 requestů po 3 v okně 0.2 s → min. 3 čekací okna


async def test_guard_dedups_identical_concurrent_requests() -> None:
    client = MockHistoricalClient()
    guard = PacingGuard(max_requests=60, window_s=600)

    results = await asyncio.gather(
        *(
            guard.run(key=("ES", TODAY), func=lambda: client.fetch_day_bars("ES", TODAY))
            for _ in range(5)
        )
    )

    assert len(client.calls) == 1  # jediný skutečný request, výsledek sdílen
    assert all(len(bars) == len(results[0]) for bars in results)


async def test_guard_priority_orders_waiting_requests() -> None:
    client = MockHistoricalClient(max_requests=1, window_s=0.05)
    guard = PacingGuard(max_requests=1, window_s=0.05)

    async def request(name: str, day: dt.date, priority: int) -> None:
        await guard.run(key=name, func=lambda: client.fetch_day_bars("ES", day), priority=priority)

    first = asyncio.create_task(request("first", TODAY, 0))
    await asyncio.sleep(0.01)  # první drží jediný slot okna
    low = asyncio.create_task(request("low", TODAY - dt.timedelta(days=2), 5))
    await asyncio.sleep(0.01)
    high = asyncio.create_task(request("high", TODAY - dt.timedelta(days=1), 0))
    await asyncio.gather(first, low, high)

    days_in_order = [day for _, day in client.calls]
    # Vysoká priorita předběhla dřív zařazený low-priority request
    assert days_in_order == [TODAY, TODAY - dt.timedelta(days=1), TODAY - dt.timedelta(days=2)]


# ── Backfill (AC issue #10) ────────────────────────────────────────


async def test_backfill_14_days_without_pacing_violation() -> None:
    client = MockHistoricalClient(max_requests=60, window_s=600, bars_per_day=5)
    guard = PacingGuard(max_requests=60, window_s=600)
    backfiller = UnderlyingBackfiller(client, guard, Settings())

    result = await backfiller.backfill("ES", TODAY)

    # AC: backfill 14 dní (+ aktuální den) proběhne bez pacing violation
    assert len(result) == 15
    assert all(len(bars) == 5 for bars in result.values())
    assert len(client.calls) == 15
    assert client.calls[0] == ("ES", TODAY)  # aktuální den má prioritu 0 → jde první


async def test_backfill_respects_tight_pacing_limit() -> None:
    # Limit těsnější než počet dní: guard musí requesty rozprostřít, žádný nesmí spadnout
    client = MockHistoricalClient(max_requests=4, window_s=0.1, bars_per_day=1)
    guard = PacingGuard(max_requests=4, window_s=0.1)
    backfiller = UnderlyingBackfiller(client, guard, Settings())

    result = await backfiller.backfill("ES", TODAY)

    assert len(result) == 15
    assert len(client.calls) == 15
