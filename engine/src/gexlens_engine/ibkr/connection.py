"""ConnectionManager (SPEC 3.1): jediné spojení na TWS/Gateway, watchdog s heartbeatem,
automatický reconnect s exponenciálním backoffem a fail-fast na delayed data.

Stavový model pro UI: connecting → connected → (výpadek) reconnecting → connected …;
delayed data nebo chybové kódy live subskripce přepnou stav na error — engine nikdy
tiše nepokračuje nad delayed daty (Greeks z nich nejsou spolehlivé).
"""

import asyncio
import contextlib
import enum
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from gexlens_engine.config import Settings

logger = logging.getLogger(__name__)

LIVE_MARKET_DATA_TYPE = 1
# Kódy TWS signalizující, že live data nejsou k dispozici (subskripce chybí / delayed)
DELAYED_DATA_ERROR_CODES = frozenset({354, 10167, 10197})


class ConnectionState(enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


@dataclass(frozen=True)
class StatusEvent:
    """Stavová událost pro API/UI (SPEC 3.7 — indikátor Connected/Reconnecting/… + port)."""

    state: ConnectionState
    detail: str
    port: int
    ts: float


class IBClientLike(Protocol):
    """Minimální rozhraní ib_async.IB, které ConnectionManager potřebuje.

    Testy používají `gexlens_engine.ibkr.mock.MockIB` (CLAUDE.md: CI nikdy proti live API).
    """

    # Jména metod záměrně kopírují camelCase API ib_async; návratové typy jsou
    # záměrně volné (object/Awaitable), aby protokol strukturálně seděl na IB
    def connectAsync(
        self,
        host: str,
        port: int,
        clientId: int,
        timeout: float,
    ) -> Awaitable[object]: ...

    def disconnect(self) -> object: ...

    def isConnected(self) -> bool: ...

    def reqMarketDataType(self, marketDataType: int) -> None: ...

    def reqCurrentTimeAsync(self) -> Awaitable[object]: ...


StatusCallback = Callable[[StatusEvent], None]
ResubscribeCallback = Callable[[], Awaitable[None]]


class ConnectionManager:
    """Drží jediné spojení, hlídá ho a po každém (re)connectu obnoví subskripce."""

    def __init__(
        self,
        client: IBClientLike,
        settings: Settings,
        *,
        heartbeat_interval_s: float = 10.0,
        heartbeat_timeout_s: float = 5.0,
    ) -> None:
        self._client = client
        self._settings = settings
        self._heartbeat_interval_s = heartbeat_interval_s
        self._heartbeat_timeout_s = heartbeat_timeout_s
        self._state = ConnectionState.DISCONNECTED
        self._history: list[StatusEvent] = []
        self._status_callbacks: list[StatusCallback] = []
        self._resubscribe_callbacks: list[ResubscribeCallback] = []
        self._backoff_history: list[float] = []
        self._supervisor: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def history(self) -> list[StatusEvent]:
        """Chronologický log stavových přechodů (pro engine_status_log a UI)."""
        return list(self._history)

    @property
    def backoff_history(self) -> list[float]:
        """Použité backoff prodlevy — diagnostika a testy exponenciálního růstu."""
        return list(self._backoff_history)

    def on_status(self, callback: StatusCallback) -> None:
        self._status_callbacks.append(callback)

    def on_resubscribe(self, callback: ResubscribeCallback) -> None:
        """Registrace plné resubskripce; volá se po každém úspěšném (re)connectu."""
        self._resubscribe_callbacks.append(callback)

    async def start(self) -> None:
        self._stopping = False
        self._supervisor = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        self._stopping = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        self._client.disconnect()
        self._set_state(ConnectionState.DISCONNECTED, "zastaveno")

    def report_market_data_type(self, market_data_type: int) -> None:
        """Fail-fast na delayed data: cokoli jiného než live (1) je chybový stav."""
        if market_data_type != LIVE_MARKET_DATA_TYPE:
            self._set_state(
                ConnectionState.ERROR,
                f"delayed market data (typ {market_data_type}) — engine odmítá pokračovat",
            )

    def report_error(self, code: int, message: str) -> None:
        """Zpracování chybových kódů TWS relevantních pro dostupnost live dat."""
        if code in DELAYED_DATA_ERROR_CODES:
            self._set_state(ConnectionState.ERROR, f"IBKR error {code}: {message}")

    def _set_state(self, state: ConnectionState, detail: str) -> None:
        self._state = state
        event = StatusEvent(
            state=state, detail=detail, port=self._settings.ibkr_port, ts=time.time()
        )
        self._history.append(event)
        logger.info("IBKR stav: %s (%s)", state.value, detail)
        for callback in self._status_callbacks:
            callback(event)

    async def _supervise(self) -> None:
        backoff = self._settings.reconnect_backoff_base_s
        first_attempt = True
        while not self._stopping:
            self._set_state(
                ConnectionState.CONNECTING if first_attempt else ConnectionState.RECONNECTING,
                f"připojuji na {self._settings.ibkr_host}:{self._settings.ibkr_port}",
            )
            try:
                await self._client.connectAsync(
                    self._settings.ibkr_host,
                    self._settings.ibkr_port,
                    clientId=self._settings.ibkr_client_id,
                    timeout=self._settings.connect_timeout_s,
                )
            except Exception as exc:
                self._backoff_history.append(backoff)
                self._set_state(
                    ConnectionState.RECONNECTING,
                    f"připojení selhalo ({exc}); další pokus za {backoff:g} s",
                )
                first_attempt = False
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._settings.reconnect_backoff_max_s)
                continue

            backoff = self._settings.reconnect_backoff_base_s
            first_attempt = False
            self._client.reqMarketDataType(LIVE_MARKET_DATA_TYPE)
            for resubscribe in self._resubscribe_callbacks:
                await resubscribe()
            self._set_state(ConnectionState.CONNECTED, "spojení navázáno, subskripce obnoveny")

            await self._monitor()
            if not self._stopping:
                self._set_state(ConnectionState.RECONNECTING, "spojení ztraceno")

    async def _monitor(self) -> None:
        """Heartbeat: periodicky ověřuje spojení; při výpadku se vrací supervisoru."""
        while not self._stopping:
            await asyncio.sleep(self._heartbeat_interval_s)
            if self._stopping:
                return
            if not self._client.isConnected():
                return
            try:
                await asyncio.wait_for(
                    self._client.reqCurrentTimeAsync(), timeout=self._heartbeat_timeout_s
                )
            except Exception:
                # Mrtvé spojení (socket visí) — tvrdý disconnect a nechat reconnect logiku běžet
                self._client.disconnect()
                return
