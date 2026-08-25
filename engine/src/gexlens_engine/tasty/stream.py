"""DXLink klient (#613): SETUP → AUTH → FEED kanál, keepalive, auto-retry.

Poznatky ze sondy #612 zadrátované jako konvence:
- server odpojuje bez klientského KEEPALIVE → posílá se à 25 s,
- 6 000+ symbolů v jedné subskripci bez degradace — subskripce se přesto
  posílají po dávkách 500, ať jednotlivá zpráva není obří,
- SDK auto-retry neexistuje → vlastní exponenciální backoff (1→60 s);
  po reconnectu se obnovuje celá subskripce (server stav nedrží).

Klient je čistý přenos: eventy předává callbacku, žádné parsování hodnot
nad rámec COMPACT rozbalení (typ, pole dle EVENT_FIELDS).
"""

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable

import websockets

logger = logging.getLogger(__name__)

#: Rozestup mezi dávkami subskripce (#845). Server odmítá „Your subscription
#: rate is too high", když dávky letí hned za sebou — odmítnuté symboly pak
#: tiše mlčely a vypadalo to jako chybějící data u konkrétního kontraktu.
SUBSCRIPTION_PAUSE_S = 0.25

#: Typy zpráv, které protokol posílá běžně — nelogují se (#845)
_EXPECTED_TYPES = frozenset({"SETUP", "AUTH_STATE", "CHANNEL_OPENED", "FEED_CONFIG", "KEEPALIVE"})
KEEPALIVE_INTERVAL_S = 25.0
SUBSCRIPTION_BATCH = 500
_BACKOFF_START_S = 1.0
_BACKOFF_MAX_S = 60.0

#: Pole eventů v COMPACT formátu — pořadí je smlouva s FEED_SETUP
EVENT_FIELDS: dict[str, list[str]] = {
    "Quote": ["eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize"],
    "Greeks": ["eventSymbol", "volatility", "delta", "gamma", "theta", "vega", "price"],
    "Summary": ["eventSymbol", "openInterest", "dayOpenPrice", "prevDayClosePrice"],
    "TimeAndSale": [
        "eventSymbol",
        "time",
        "price",
        "size",
        "aggressorSide",
        "spreadLeg",
        "extendedTradingHours",
    ],
}

#: Callback: (typ eventu, hodnoty jednoho záznamu dle EVENT_FIELDS pořadí)
EventCallback = Callable[[str, list[object]], None]
#: Zdroj čerstvého (dxlink_url, quote_token) — token platí ~24 h, po
#: reconnectu se žádá nový
TokenSource = Callable[[], Awaitable[tuple[str, str]]]


class DxLinkStream:
    """Trvalé DXLink spojení s vlastní obnovou; eventy tečou do callbacku."""

    def __init__(
        self,
        token_source: TokenSource,
        on_event: EventCallback,
        *,
        events: tuple[str, ...] = ("Quote", "Greeks", "Summary", "TimeAndSale"),
    ) -> None:
        self._token_source = token_source
        self._on_event = on_event
        self._events = events
        self._symbols: set[str] = set()
        self._lock = asyncio.Lock()
        self._ws: websockets.ClientConnection | None = None
        #: Diagnostika pro shadow report: kolik reconnectů proběhlo
        self.reconnects = 0
        #: Počet ERROR zpráv ze serveru (#845) a poslední text
        self._priority: frozenset[str] = frozenset()
        self.errors = 0
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        """Stav spojení pro /status (#706) — True mezi handshake a výpadkem."""
        return self._ws is not None

    async def set_symbols(self, symbols: set[str]) -> None:
        """Cílová množina symbolů; rozdíl se přihlásí/odhlásí za běhu."""
        async with self._lock:
            added = symbols - self._symbols
            removed = self._symbols - symbols
            self._symbols = set(symbols)
            if self._ws is not None:
                try:
                    await self._send_subscription(add=added, remove=removed)
                except Exception:
                    logger.exception("Změna subskripce selhala — obnoví ji reconnect")

    async def run(self, stop: asyncio.Event) -> None:
        """Hlavní smyčka: připojit, subskribovat, číst; při pádu backoff."""
        backoff = _BACKOFF_START_S
        while not stop.is_set():
            try:
                await self._connect_and_read(stop)
                backoff = _BACKOFF_START_S
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "DXLink spojení spadlo (%s: %s) — reconnect za %.0f s",
                    type(error).__name__,
                    error,
                    backoff,
                )
                self.reconnects += 1
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)

    # ── vnitřek ────────────────────────────────────────────────────

    async def _send(self, payload: dict[str, object]) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps(payload))

    async def _recv_until(self, message_type: str, timeout: float = 10.0) -> dict[str, object]:
        assert self._ws is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=deadline - time.monotonic())
            message = json.loads(raw)
            if message.get("type") == message_type:
                return dict(message)
            if message.get("type") == "ERROR":
                raise RuntimeError(f"DXLink ERROR: {message}")
        raise TimeoutError(message_type)

    def set_priority(self, symbols: frozenset[str]) -> None:
        """Symboly, které se subskribují jako první (#845).

        Při rate limitu server část dávek odmítne, takže na pořadí záleží:
        podklady ES/NQ nesou cenu a CVD, kdežto o jeden opční strike navíc
        v křídle nejde.
        """
        self._priority = symbols

    async def _send_subscription(
        self, add: set[str], remove: frozenset[str] | set[str] = frozenset()
    ) -> None:
        # Prioritní symboly první, zbytek abecedně (stabilní pořadí)
        ordered_add = sorted(add, key=lambda symbol: (symbol not in self._priority, symbol))
        entries_add = [
            {"type": event, "symbol": symbol} for symbol in ordered_add for event in self._events
        ]
        entries_remove = [
            {"type": event, "symbol": symbol} for symbol in sorted(remove) for event in self._events
        ]
        for offset in range(0, max(len(entries_add), len(entries_remove)), SUBSCRIPTION_BATCH):
            payload: dict[str, object] = {"type": "FEED_SUBSCRIPTION", "channel": 1}
            batch_add = entries_add[offset : offset + SUBSCRIPTION_BATCH]
            batch_remove = entries_remove[offset : offset + SUBSCRIPTION_BATCH]
            if batch_add:
                payload["add"] = batch_add
            if batch_remove:
                payload["remove"] = batch_remove
            if batch_add or batch_remove:
                await self._send(payload)
                # Bez rozestupu server dávky odmítá (#845) a odmítnuté
                # symboly pak tiše mlčí — viz `Your subscription rate is
                # too high` v produkci při ~4 700 symbolech
                await asyncio.sleep(SUBSCRIPTION_PAUSE_S)

    async def _connect_and_read(self, stop: asyncio.Event) -> None:
        url, token = await self._token_source()
        async with websockets.connect(url, max_size=2**24) as ws:
            self._ws = ws
            try:
                await self._send(
                    {
                        "type": "SETUP",
                        "channel": 0,
                        "version": "0.1-gexlens",
                        "keepaliveTimeout": 60,
                        "acceptKeepaliveTimeout": 60,
                    }
                )
                await self._recv_until("SETUP")
                state = await self._recv_until("AUTH_STATE")
                if state.get("state") == "UNAUTHORIZED":
                    await self._send({"type": "AUTH", "channel": 0, "token": token})
                    state = await self._recv_until("AUTH_STATE")
                if state.get("state") != "AUTHORIZED":
                    raise RuntimeError(f"DXLink autorizace selhala: {state}")
                await self._send(
                    {
                        "type": "CHANNEL_REQUEST",
                        "channel": 1,
                        "service": "FEED",
                        "parameters": {"contract": "AUTO"},
                    }
                )
                await self._recv_until("CHANNEL_OPENED")
                await self._send(
                    {
                        "type": "FEED_SETUP",
                        "channel": 1,
                        "acceptAggregationPeriod": 0,
                        "acceptDataFormat": "COMPACT",
                        "acceptEventFields": {name: EVENT_FIELDS[name] for name in self._events},
                    }
                )
                await self._recv_until("FEED_CONFIG")
                async with self._lock:
                    await self._send_subscription(add=set(self._symbols))

                last_keepalive = time.monotonic()
                while not stop.is_set():
                    if time.monotonic() - last_keepalive > KEEPALIVE_INTERVAL_S:
                        await self._send({"type": "KEEPALIVE", "channel": 0})
                        last_keepalive = time.monotonic()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except TimeoutError:
                        continue
                    message = json.loads(raw)
                    msg_type = str(message.get("type") or "")
                    if msg_type == "FEED_DATA":
                        self._dispatch(message.get("data", []))
                        continue
                    # Cokoli jiného se dřív tiše zahodilo — včetně ERROR.
                    # Tiché odmítnutí subskripce pak bylo nerozeznatelné od
                    # „symbol mlčí" (#845; totéž stálo za nedělním hledáním
                    # v #616, kdy ES extended mlčelo po přetečení kapacity).
                    if msg_type == "ERROR":
                        self.errors += 1
                        self.last_error = str(message.get("message") or message)
                        logger.error("DXLink ERROR: %s", message)
                    elif msg_type not in _EXPECTED_TYPES:
                        logger.info("DXLink neznámá zpráva %s: %s", msg_type, message)
            finally:
                self._ws = None

    def _dispatch(self, data: list[object]) -> None:
        for index in range(0, len(data) - 1, 2):
            event_type = data[index]
            values = data[index + 1]
            fields = EVENT_FIELDS.get(str(event_type))
            if fields is None or not isinstance(values, list):
                continue
            width = len(fields)
            for offset in range(0, len(values) - width + 1, width):
                self._on_event(str(event_type), values[offset : offset + width])
