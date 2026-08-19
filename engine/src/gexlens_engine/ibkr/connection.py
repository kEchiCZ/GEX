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
# Kódy TWS = engine dostává DELAYED data (SPEC 3.1: fail-fast, Greeks z nich nejsou
# spolehlivé). Patří sem jen 10167 „Displaying delayed market data", který delayed
# data přímo potvrzuje.
#
# NEPATŘÍ sem:
# * 354 („not subscribed") — per-request, chodí i s platnou subskripcí při výpadku
#   farmy; hlídá ho `subscription.py` (#417),
# * 10197 („No market data during competing live session") — viz níže (#451).
DELAYED_DATA_ERROR_CODES = frozenset({10167})

# Konkurenční relace: stejný účet je přihlášený jinde (mobil, Client Portal, druhá
# TWS) a přetahuje si market data. NENÍ to potvrzení delayed dat — naměřeno 4. 8.:
# kód chodil 2× za minutu na trvalých spot subskripcích, zatímco bary i Greeks
# tekly kompletní. Fail-fast kvůli němu překlápěl stav spojení do `error` dvakrát
# za minutu, přestože bylo všechno v pořádku. Řeší se alertem, ne stavem (#451).
COMPETING_SESSION_ERROR_CODE = 10197


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
#: Hlášení dlouho trvajícího odpojení (#770); argument = jak dlouho už spojení není
StallCallback = Callable[[float], None]


class ConnectionManager:
    """Drží jediné spojení, hlídá ho a po každém (re)connectu obnoví subskripce."""

    def __init__(
        self,
        client: IBClientLike,
        settings: Settings,
        *,
        heartbeat_interval_s: float = 10.0,
        heartbeat_timeout_s: float = 5.0,
        watchdog_interval_s: float = 30.0,
    ) -> None:
        self._client = client
        self._settings = settings
        self._heartbeat_interval_s = heartbeat_interval_s
        self._heartbeat_timeout_s = heartbeat_timeout_s
        self._watchdog_interval_s = watchdog_interval_s
        self._state = ConnectionState.DISCONNECTED
        self._history: list[StatusEvent] = []
        self._status_callbacks: list[StatusCallback] = []
        self._resubscribe_callbacks: list[ResubscribeCallback] = []
        self._stall_callbacks: list[StallCallback] = []
        self._backoff_history: list[float] = []
        self._supervisor: asyncio.Task[None] | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._stopping = False
        # Od kdy jsme mimo stav connected (monotonic; None = spojení drží).
        # Měří se monotonicky schválně — letní čas ani srovnání hodin nesmí
        # posunout práh hlášení.
        self._offline_since: float | None = time.monotonic()
        self._last_stall_report: float | None = None
        #: Kolikrát watchdog vzkřísil mrtvého supervisora (diagnostika, #770)
        self._supervisor_restarts = 0

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

    def on_stall(self, callback: StallCallback) -> None:
        """Registrace hlášení, že spojení chybí podezřele dlouho (#770)."""
        self._stall_callbacks.append(callback)

    @property
    def supervisor_restarts(self) -> int:
        """Kolikrát musel watchdog vzkřísit supervisora — nenulová hodnota je nález."""
        return self._supervisor_restarts

    @property
    def offline_for_s(self) -> float | None:
        """Jak dlouho spojení chybí v sekundách; None = spojení drží (#770).

        Doba, ne timestamp — počítá se z monotonic, takže ji neposune změna
        hodin ani DST; na wall-clock „od kdy" si ji odečte konzument.
        """
        if self._offline_since is None:
            return None
        return time.monotonic() - self._offline_since

    async def start(self) -> None:
        self._stopping = False
        self._offline_since = time.monotonic()
        self._last_stall_report = None
        self._supervisor = asyncio.create_task(self._supervise())
        # Watchdog běží ZÁMĚRNĚ mimo supervisora (#770). Kdyby visel na téže
        # úloze, sdílel by i její osud — a přesně to se 18. 8. stalo: reconnect
        # umlkl, engine zůstal osm hodin offline a nikde to nezaznělo.
        self._watchdog = asyncio.create_task(self._watch())

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._watchdog, self._supervisor):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._watchdog = None
        self._supervisor = None
        self._client.disconnect()
        self._set_state(ConnectionState.DISCONNECTED, "zastaveno")

    async def resubscribe_now(self) -> bool:
        """Cílená obnova subskripcí BEZ reconnectu (#517 fáze B).

        Volá ji aktivní sonda, když farma data dodává, ale naše subskripce
        umřely potichu — přesně incident 26.–27. 7. (15 h zmrzlé ATM greeks
        při tekoucích cenách). Běží týž řetěz callbacků jako po reconnectu,
        takže je to jedna ohraničená akce, ne bouře per kontrakt.

        Vrací True při úspěchu. Selhání znamená spojení, přes které nejde
        obnovit data → disconnect a nechá se supervisor přepojit standardní
        cestou (heartbeat výpadek pozná).
        """
        for resubscribe in self._resubscribe_callbacks:
            try:
                await resubscribe()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._set_state(
                    ConnectionState.RECONNECTING,
                    f"vynucená obnova subskripcí selhala ({exc}) — přepojuji",
                )
                with contextlib.suppress(Exception):
                    self._client.disconnect()
                return False
        logger.info(
            "Vynucená obnova subskripcí proběhla (%d callbacků)",
            len(self._resubscribe_callbacks),
        )
        return True

    def report_market_data_type(self, market_data_type: int) -> None:
        """Fail-fast na delayed data: cokoli jiného než live (1) je chybový stav."""
        if market_data_type != LIVE_MARKET_DATA_TYPE:
            self._set_state(
                ConnectionState.ERROR,
                f"delayed market data (typ {market_data_type}) — engine odmítá pokračovat",
            )

    def report_error(self, code: int, message: str) -> None:
        """Zpracování chybových kódů TWS relevantních pro dostupnost live dat.

        Volá se z `ib.errorEvent` (zapojeno v `__main__`); reaguje jen na kódy
        znamenající delayed data. Per-request chyby subskripce (354) sem nepatří,
        viz `DELAYED_DATA_ERROR_CODES`.
        """
        if code in DELAYED_DATA_ERROR_CODES:
            self._set_state(ConnectionState.ERROR, f"IBKR error {code}: {message}")

    def _set_state(self, state: ConnectionState, detail: str) -> None:
        self._state = state
        if state is ConnectionState.CONNECTED:
            self._offline_since = None
            self._last_stall_report = None
        elif self._offline_since is None:
            self._offline_since = time.monotonic()
        event = StatusEvent(
            state=state, detail=detail, port=self._settings.ibkr_port, ts=time.time()
        )
        self._history.append(event)
        logger.info("IBKR stav: %s (%s)", state.value, detail)
        for callback in self._status_callbacks:
            # Odběratel stavu (publikace do API, UI) nesmí shodit smyčku spojení —
            # výjimka odsud dřív propadla až do supervisora a zabila reconnect (#770)
            try:
                callback(event)
            except Exception:
                logger.exception("Odběratel stavu spojení selhal (stav %s)", state.value)

    async def _supervise(self) -> None:
        """Nekonečná smyčka připojování. Skončit smí JEN na `stop()` (#770).

        Každá iterace je proto obalená — jakákoli neočekávaná výjimka (odběratel
        stavu, selhaná resubskripce, chyba v ib_async) se zaloguje a smyčka jede
        dál. Dokud tady bylo holé tělo, stačila jediná výjimka mimo `connectAsync`
        a celý reconnect zmizel bez jediného řádku v logu.
        """
        backoff = self._settings.reconnect_backoff_base_s
        first_attempt = True
        while not self._stopping:
            try:
                connected = await self._try_connect(first_attempt, backoff)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Neočekávaná chyba ve smyčce spojení — pokračuji dál")
                connected = False
            first_attempt = False
            if not connected:
                self._backoff_history.append(backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._settings.reconnect_backoff_max_s)
                continue

            backoff = self._settings.reconnect_backoff_base_s
            try:
                await self._monitor()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Heartbeat spojení selhal — přepínám na reconnect")
            if not self._stopping:
                self._set_state(ConnectionState.RECONNECTING, "spojení ztraceno")

    async def _try_connect(self, first_attempt: bool, backoff: float) -> bool:
        """Jeden pokus o připojení včetně resubskripcí. True = spojení drží."""
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
            self._set_state(
                ConnectionState.RECONNECTING,
                f"připojení selhalo ({exc}); další pokus za {backoff:g} s",
            )
            return False

        self._client.reqMarketDataType(LIVE_MARKET_DATA_TYPE)
        for resubscribe in self._resubscribe_callbacks:
            # Selhaná resubskripce znamená spojení bez dat — zpátky na reconnect.
            # Dřív výjimka odsud propadla ze smyčky a engine zůstal viset offline.
            try:
                await resubscribe()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._set_state(
                    ConnectionState.RECONNECTING,
                    f"obnova subskripcí selhala ({exc}) — připojuji znovu",
                )
                with contextlib.suppress(Exception):
                    self._client.disconnect()
                return False

        self._set_state(ConnectionState.CONNECTED, "spojení navázáno, subskripce obnoveny")
        return True

    async def _watch(self) -> None:
        """Dozor nad supervisorem a nad délkou výpadku (#770).

        Dvě věci, které 18. 8. chyběly:
        1. Když supervisor z jakéhokoli důvodu skončí, nikdo si toho nevšiml —
           `create_task` výjimku jen odloží a engine tiše zůstane offline.
           Watchdog ho vzkřísí.
        2. Osm hodin bez spojení neprodukovalo žádné hlášení. Po překročení
           `reconnect_stall_alert_s` jde ERROR do logu a hlášení odběratelům
           (zvoneček), a opakuje se, dokud se spojení nevrátí.
        """
        while not self._stopping:
            await asyncio.sleep(self._watchdog_interval_s)
            if self._stopping:
                return
            self._revive_supervisor_if_dead()
            self._report_stall_if_due()

    def _revive_supervisor_if_dead(self) -> None:
        task = self._supervisor
        if task is None or not task.done():
            return
        # Výjimku si vyzvedneme, ať se neztratí v „exception was never retrieved"
        exc = None if task.cancelled() else task.exception()
        self._supervisor_restarts += 1
        logger.error(
            "Smyčka spojení skončila (%s) — startuji ji znovu (už po %d.)",
            exc if exc is not None else "bez výjimky",
            self._supervisor_restarts,
        )
        self._supervisor = asyncio.create_task(self._supervise())

    def _report_stall_if_due(self) -> None:
        if self._offline_since is None:
            return
        now = time.monotonic()
        offline_s = now - self._offline_since
        threshold = self._settings.reconnect_stall_alert_s
        if offline_s < threshold:
            return
        if self._last_stall_report is not None and now - self._last_stall_report < threshold:
            return
        self._last_stall_report = now
        logger.error(
            "IBKR spojení chybí už %.0f s (stav %s, %s:%d) — sběr dat stojí",
            offline_s,
            self._state.value,
            self._settings.ibkr_host,
            self._settings.ibkr_port,
        )
        for callback in self._stall_callbacks:
            try:
                callback(offline_s)
            except Exception:
                logger.exception("Hlášení výpadku spojení selhalo")

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
