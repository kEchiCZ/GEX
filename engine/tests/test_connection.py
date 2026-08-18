"""Testy ConnectionManageru (issue #4) nad MockIB: connect, reconnect, backoff, delayed data."""

import asyncio
import contextlib

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


@pytest.mark.parametrize("code", [10167])
async def test_delayed_error_codes_are_error_state(code: int) -> None:
    client = MockIB()
    manager = manager_with(client)
    await manager.start()
    await wait_for_state(manager, ConnectionState.CONNECTED)

    manager.report_error(code, "mock hláška TWS")
    assert manager.state is ConnectionState.ERROR
    await manager.stop()


async def test_competing_session_does_not_change_state() -> None:
    """Konkurenční relace (10197) není potvrzení delayed dat (#451).

    Naměřeno 4. 8.: kód chodil 2× za minutu na trvalých spot subskripcích,
    zatímco bary i Greeks tekly kompletní. Fail-fast kvůli němu překlápěl stav
    do `error` dvakrát za minutu, přestože bylo všechno v pořádku.
    """
    client = MockIB()
    manager = manager_with(client)
    await manager.start()
    await wait_for_state(manager, ConnectionState.CONNECTED)

    manager.report_error(10197, "No market data during competing live session")
    assert manager.state is ConnectionState.CONNECTED
    await manager.stop()


async def test_not_subscribed_error_does_not_change_state() -> None:
    """Error 354 je per-request a chodí i s platnou subskripcí (#417).

    Do #417 shazoval stav na ERROR — jedna přechodná chyba ze sweepu (~1 ze 70k)
    by tak z výpadku farmy udělala trvalou chybu spojení. Hlídá ho
    `SubscriptionErrorTracker`, ne stavový automat.
    """
    client = MockIB()
    manager = manager_with(client)
    await manager.start()
    await wait_for_state(manager, ConnectionState.CONNECTED)

    manager.report_error(354, "Requested market data is not subscribed")
    assert manager.state is ConnectionState.CONNECTED
    await manager.stop()


async def test_unrelated_error_codes_do_not_change_state() -> None:
    client = MockIB()
    manager = manager_with(client)
    await manager.start()
    await wait_for_state(manager, ConnectionState.CONNECTED)

    manager.report_error(2104, "Market data farm connection is OK")
    assert manager.state is ConnectionState.CONNECTED
    await manager.stop()


# --- Odolnost reconnectu (#770): smyčka nesmí umřít, dlouhý výpadek se musí ozvat ---


def manager_with_watchdog(
    client: MockIB, *, stall_alert_s: float = 0.05, watchdog_interval_s: float = 0.01
) -> ConnectionManager:
    settings = fast_settings().model_copy(update={"reconnect_stall_alert_s": stall_alert_s})
    return ConnectionManager(
        client,
        settings,
        heartbeat_interval_s=0.02,
        heartbeat_timeout_s=0.05,
        watchdog_interval_s=watchdog_interval_s,
    )


async def test_status_callback_exception_does_not_kill_loop() -> None:
    """Padlý odběratel stavu zabíjel reconnect — výjimka propadla ze `_set_state`
    až do supervisora a engine zůstal viset offline (#770)."""
    client = MockIB()
    manager = manager_with_watchdog(client)

    def broken(_event: object) -> None:
        raise RuntimeError("odběratel spadl")

    manager.on_status(broken)
    await manager.start()
    await wait_for_state(manager, ConnectionState.CONNECTED)

    client.drop_connection()
    await wait_for_state(manager, ConnectionState.CONNECTED)  # sám se vrátil
    assert manager.supervisor_restarts == 0  # ani nebylo potřeba křísit
    await manager.stop()


async def test_failed_resubscribe_returns_to_reconnect() -> None:
    """Spojení bez obnovených subskripcí je k ničemu — patří zpátky na reconnect,
    ne k pádu smyčky."""
    client = MockIB()
    manager = manager_with_watchdog(client)
    attempts = 0

    async def resubscribe() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("subskripce selhala")

    manager.on_resubscribe(resubscribe)
    await manager.start()
    await wait_for_state(manager, ConnectionState.CONNECTED)

    assert attempts >= 2  # první pokus selhal, druhý prošel
    details = [event.detail for event in manager.history]
    assert any("obnova subskripcí selhala" in detail for detail in details)
    await manager.stop()


async def test_watchdog_revives_dead_supervisor() -> None:
    """Jádro #770: i kdyby smyčka spojení z jakéhokoli důvodu skončila, watchdog
    ji nastartuje znovu. Do teď tam engine zůstal osm hodin viset."""
    client = MockIB()
    manager = manager_with_watchdog(client)
    await manager.start()
    await wait_for_state(manager, ConnectionState.CONNECTED)

    # Simulace přesně toho, co se stalo v provozu: supervisor přestane existovat
    supervisor = manager._supervisor  # noqa: SLF001 — test sahá na vnitřek schválně
    assert supervisor is not None
    supervisor.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await supervisor

    async with asyncio.timeout(2.0):
        while manager.supervisor_restarts == 0:
            await asyncio.sleep(0.005)
    await wait_for_state(manager, ConnectionState.CONNECTED)
    await manager.stop()


async def test_long_outage_is_reported() -> None:
    """Osmihodinový výpadek nikde nezazněl (#770). Po překročení prahu musí
    přijít hlášení — a opakovat se, dokud spojení nechybí."""
    client = MockIB(fail_connects=1000)  # TWS je trvale dole
    manager = manager_with_watchdog(client, stall_alert_s=0.05)
    reports: list[float] = []
    manager.on_stall(reports.append)

    await manager.start()
    async with asyncio.timeout(3.0):
        while len(reports) < 2:
            await asyncio.sleep(0.005)

    assert reports[0] >= 0.05
    assert reports[1] > reports[0]  # hlášení se opakuje a čas roste
    await manager.stop()


async def test_no_report_while_connection_holds() -> None:
    """Falešné poplachy: dokud spojení drží, nesmí se ozvat nic."""
    client = MockIB()
    manager = manager_with_watchdog(client, stall_alert_s=0.02)
    reports: list[float] = []
    manager.on_stall(reports.append)

    await manager.start()
    await wait_for_state(manager, ConnectionState.CONNECTED)
    await asyncio.sleep(0.15)  # několikanásobek prahu i intervalu watchdogu

    assert reports == []
    await manager.stop()
