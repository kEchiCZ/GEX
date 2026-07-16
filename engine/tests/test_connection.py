"""Testy ConnectionManageru (issue #4) nad MockIB: connect, reconnect, backoff, delayed data."""

import asyncio

import pytest

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.connection import ConnectionManager, ConnectionState
from gexlens_engine.ibkr.mock import MockIB


def fast_settings() -> Settings:
    return Settings(
        reconnect_backoff_base_s=0.01,
        reconnect_backoff_max_s=0.04,
        connect_timeout_s=0.5,
    )


def manager_with(client: MockIB) -> ConnectionManager:
    return ConnectionManager(
        client, fast_settings(), heartbeat_interval_s=0.02, heartbeat_timeout_s=0.05
    )


async def wait_for_state(
    manager: ConnectionManager, state: ConnectionState, timeout: float = 2.0
) -> None:
    async with asyncio.timeout(timeout):
        while manager.state is not state:
            await asyncio.sleep(0.005)


async def test_connect_flow_sets_live_and_resubscribes() -> None:
    client = MockIB()
    manager = manager_with(client)
    resubscribed = 0

    async def resubscribe() -> None:
        nonlocal resubscribed
        resubscribed += 1

    manager.on_resubscribe(resubscribe)
    await manager.start()
    await wait_for_state(manager, ConnectionState.CONNECTED)

    assert client.market_data_type_requests == [1]  # vynucený live režim (SPEC 3.1)
    assert resubscribed == 1
    await manager.stop()
    assert manager.state is ConnectionState.DISCONNECTED


async def test_reconnect_after_connection_loss() -> None:
    client = MockIB()
    manager = manager_with(client)
    resubscribed = 0

    async def resubscribe() -> None:
        nonlocal resubscribed
        resubscribed += 1

    manager.on_resubscribe(resubscribe)
    await manager.start()
    await wait_for_state(manager, ConnectionState.CONNECTED)

    client.drop_connection()  # kill TWS
    async with asyncio.timeout(2.0):
        while resubscribed < 2:
            await asyncio.sleep(0.005)
    await wait_for_state(manager, ConnectionState.CONNECTED)

    # AC: přechody Connected → Reconnecting → Connected
    states = [event.state for event in manager.history]
    first_connected = states.index(ConnectionState.CONNECTED)
    assert ConnectionState.RECONNECTING in states[first_connected:]
    assert states[-1] is ConnectionState.CONNECTED
    assert client.market_data_type_requests == [1, 1]
    await manager.stop()


async def test_backoff_grows_exponentially_and_caps() -> None:
    client = MockIB(fail_connects=4)
    manager = manager_with(client)
    await manager.start()
    await wait_for_state(manager, ConnectionState.CONNECTED)

    assert manager.backoff_history == [0.01, 0.02, 0.04, 0.04]  # 2× růst, strop max
    await manager.stop()


async def test_heartbeat_detects_dead_socket() -> None:
    client = MockIB()
    manager = manager_with(client)
    await manager.start()
    await wait_for_state(manager, ConnectionState.CONNECTED)

    client.heartbeat_hang = True  # socket visí, isConnected stále True
    async with asyncio.timeout(2.0):
        while client.disconnect_calls == 0:
            await asyncio.sleep(0.005)
    client.heartbeat_hang = False
    await wait_for_state(manager, ConnectionState.CONNECTED)  # sám se obnovil
    await manager.stop()


async def test_delayed_market_data_is_error_state() -> None:
    client = MockIB()
    manager = manager_with(client)
    await manager.start()
    await wait_for_state(manager, ConnectionState.CONNECTED)

    manager.report_market_data_type(3)  # TWS přepnul na delayed
    assert manager.state is ConnectionState.ERROR
    assert "delayed" in manager.history[-1].detail
    await manager.stop()


@pytest.mark.parametrize("code", [354, 10167, 10197])
async def test_delayed_error_codes_are_error_state(code: int) -> None:
    client = MockIB()
    manager = manager_with(client)
    await manager.start()
    await wait_for_state(manager, ConnectionState.CONNECTED)

    manager.report_error(code, "mock hláška TWS")
    assert manager.state is ConnectionState.ERROR
    await manager.stop()


async def test_unrelated_error_codes_do_not_change_state() -> None:
    client = MockIB()
    manager = manager_with(client)
    await manager.start()
    await wait_for_state(manager, ConnectionState.CONNECTED)

    manager.report_error(2104, "Market data farm connection is OK")
    assert manager.state is ConnectionState.CONNECTED
    await manager.stop()
