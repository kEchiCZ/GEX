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
from collections.abc import Awaitable, Callable, Iterable, Mapping

import websockets

from gexlens_engine.tasty.dxlink import handshake

logger = logging.getLogger(__name__)

#: Rozestup mezi dávkami subskripce (#845). Server odmítá „Your subscription
#: rate is too high", když dávky letí hned za sebou — odmítnuté symboly pak
#: tiše mlčely a vypadalo to jako chybějící data u konkrétního kontraktu.
SUBSCRIPTION_PAUSE_S = 0.25

#: Strop adaptivního rozestupu po rate limitu (#863): každé odmítnutí rozestup
#: zdvojí, takže opakovaný resubscribe konverguje k tempu, které server bere.
#: Strop musí držet invariant #916: plná subskripce (~45 dávek) × strop
#: NESMÍ přesáhnout PING_TIMEOUT_S — vyšší strop by vrátil smyčku smrti.
#: Konvergenci za RTH řeší cílený heal (#936), ne pomalejší plný resubscribe.
SUBSCRIPTION_PAUSE_MAX_S = 2.0

#: Po jak dlouhém klidu (bez rate limitu) se rozestup zase POVOLUJE (#936) —
#: bez relaxace by jediná ranní špička zpomalila subskripce na celý den.
PAUSE_RELAX_AFTER_S = 120.0

#: Samoléčení po rate limitu (#863): server u odmítnuté dávky neřekne, KTERÉ
#: symboly zahodil — jediná spolehlivá oprava je poslat celou cílovou množinu
#: znovu (add je idempotentní). Čeká se na klid po poslední chybě, ať se
#: neposílá do běžícího limitu.
RATE_LIMIT_HEAL_QUIET_S = 10.0

#: Typy zpráv, které protokol posílá běžně — nelogují se (#845)
_EXPECTED_TYPES = frozenset({"SETUP", "AUTH_STATE", "CHANNEL_OPENED", "FEED_CONFIG", "KEEPALIVE"})
KEEPALIVE_INTERVAL_S = 25.0

#: Transportní ping websockets knihovny (#916). Default ping_timeout=20 s byl
#: přísnější než trvání plné subskripce (~44 dávek à 2 s ≈ 90 s po rate
#: limitu): server během subscribe bloku ping nezodpověděl včas, klient
#: spojení sám zabil a subskripce se NIKDY nedokončila — smyčka reconnectů,
#: fallback #614 bez dat. Timeout musí subskripci s rezervou přežít; mrtvé
#: spojení dál hlídá protokolový KEEPALIVE (vyjednaných 60 s) + backoff.
PING_TIMEOUT_S = 120.0
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
        heal_targets: Callable[[set[str]], set[str]] | None = None,
    ) -> None:
        self._token_source = token_source
        self._on_event = on_event
        self._events = events
        #: Cílová subskripce per symbol → eventy (#982): každý účel odebírá
        #: jen to, co čte; strop serveru je 25 000 položek symbol × event
        self._subs: dict[str, frozenset[str]] = {}
        self._lock = asyncio.Lock()
        self._ws: websockets.ClientConnection | None = None
        #: Diagnostika pro shadow report: kolik reconnectů proběhlo
        self.reconnects = 0
        #: Počet ERROR zpráv ze serveru (#845) a poslední text
        self._priority: frozenset[str] = frozenset()
        self.errors = 0
        self.last_error: str | None = None
        #: Rate limit subskripcí (#863): počet odmítnutí + stav samoléčení
        self.rate_limited = 0
        #: Přetečení stropu velikosti subskripce (#982): server odmítl dávku
        #: `Your subscription size is too big` — odmítnuté symboly mlčí
        self.size_exceeded = 0
        self.size_exceeded_ts: float | None = None
        #: Cílený heal (#936): vrátí podmnožinu mlčících symbolů; None = plný
        self._heal_targets = heal_targets
        self.heals = 0
        self._last_rate_limit_seen = 0.0
        self._pause_s = SUBSCRIPTION_PAUSE_S
        self._rate_limit_ts: float | None = None
        self._last_keepalive = 0.0

    @property
    def connected(self) -> bool:
        """Stav spojení pro /status (#706) — True mezi handshake a výpadkem."""
        return self._ws is not None

    @property
    def _symbols(self) -> set[str]:
        """Subskribované symboly (bez ohledu na eventy) — heal a diagnostika."""
        return set(self._subs)

    @_symbols.setter
    def _symbols(self, symbols: set[str]) -> None:
        """Testy a dev laboratoř: množina symbolů = všechny eventy streamu."""
        self._subs = {symbol: frozenset(self._events) for symbol in symbols}

    @property
    def entries_total(self) -> int:
        """Počet položek symbol × event v cílové subskripci (#982)."""
        return sum(len(events) for events in self._subs.values())

    async def set_symbols(self, symbols: set[str]) -> None:
        """Cílová množina symbolů se všemi eventy streamu (dev laboratoř #623)."""
        await self.set_subscriptions({symbol: self._events for symbol in symbols})

    async def set_subscriptions(self, subscriptions: Mapping[str, Iterable[str]]) -> None:
        """Cílová subskripce symbol → eventy; rozdíl se přihlásí/odhlásí za běhu.

        Rozdíl se počítá po položkách (#982): symbol, kterému ubyl jeden
        event, dostane jen `remove` toho eventu — ne odhlášení a nové
        přihlášení, které by na chvíli umlčelo i zbylé eventy.
        """
        async with self._lock:
            target = {
                symbol: frozenset(events)
                for symbol, events in subscriptions.items()
                if frozenset(events)
            }
            added: dict[str, frozenset[str]] = {}
            removed: dict[str, frozenset[str]] = {}
            for symbol, events in target.items():
                new = events - self._subs.get(symbol, frozenset())
                if new:
                    added[symbol] = new
            for symbol, events in self._subs.items():
                gone = events - target.get(symbol, frozenset())
                if gone:
                    removed[symbol] = gone
            self._subs = target
            if self._ws is not None:
                try:
                    await self._send_subscription(add=added, remove=removed)
                except Exception as error:
                    # Bez tracebacku (#937 deploy): očekávaný race s reconnectem
                    # není porucha — traceback v logu trhal deploy health check
                    logger.warning(
                        "Změna subskripce selhala (%s: %s) — obnoví ji reconnect",
                        type(error).__name__,
                        error,
                    )

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

    async def force_reconnect(self, reason: str) -> None:
        """Vynucené přepojení DXLink (#950): zavře socket, `run` se připojí znovu.

        Čtecí smyčka na zavřeném socketu vyhodí, `run` to zachytí jako pád
        spojení a projde standardní cestou včetně resubskripce — žádná druhá
        cesta k připojení, kterou by bylo potřeba udržovat.
        """
        logger.info("DXLink: vynucené přepojení (%s)", reason)
        ws = self._ws
        if ws is None:
            return
        with contextlib.suppress(Exception):
            await ws.close()

    # ── vnitřek ────────────────────────────────────────────────────

    async def _send(self, payload: dict[str, object]) -> None:
        if self._ws is None:
            # Očekávaný race: reconnect shodil _ws mezi kontrolou a odesláním.
            # Čistá výjimka bez assertu — volající ji řeší, reconnect obnoví.
            raise ConnectionError("DXLink spojení není otevřené")
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

    def _note_server_error(self, message: dict[str, object]) -> None:
        """ERROR ze serveru: počítadla + detekce rate limitu subskripcí (#863).

        Odmítnuté symboly by tiše mlčely — označí se čas pro samoléčení
        a rozestup dávek se zdvojí (se stropem), ať opakování konverguje.
        """
        self.errors += 1
        self.last_error = str(message.get("message") or message)
        logger.error("DXLink ERROR: %s", message)
        lowered = self.last_error.lower()
        if "subscription rate" in lowered:
            self.rate_limited += 1
            self._rate_limit_ts = time.monotonic()
            self._last_rate_limit_seen = self._rate_limit_ts
            self._pause_s = min(self._pause_s * 2, SUBSCRIPTION_PAUSE_MAX_S)
        elif "subscription size" in lowered:
            # Strop 25 000 položek na spojení (#982): heal tady nepomůže —
            # opakované přihlášení téže množiny přeteče znovu. Plán musí
            # zmenšit rozpočet (tasty/budget.py); tohle jen nahlas přizná stav.
            self.size_exceeded += 1
            self.size_exceeded_ts = time.monotonic()
            logger.error(
                "DXLink strop velikosti subskripce: plán %d položek (%d symbolů) — "
                "odmítnuté symboly mlčí, zmenšit rozpočet (#982)",
                self.entries_total,
                len(self._subs),
            )

    def _heal_due(self, now: float) -> bool:
        """Je čas na samoléčebný resubscribe? Až po zklidnění rate limitu."""
        return (
            self._rate_limit_ts is not None and now - self._rate_limit_ts > RATE_LIMIT_HEAL_QUIET_S
        )

    def _as_entries(
        self, subscriptions: Mapping[str, Iterable[str]] | Iterable[str]
    ) -> dict[str, frozenset[str]]:
        """Množina symbolů = všechny eventy streamu; mapa = jak je zadaná."""
        if isinstance(subscriptions, Mapping):
            return {symbol: frozenset(events) for symbol, events in subscriptions.items()}
        return {symbol: frozenset(self._events) for symbol in subscriptions}

    async def _send_subscription(
        self,
        add: Mapping[str, Iterable[str]] | Iterable[str],
        remove: Mapping[str, Iterable[str]] | Iterable[str] = frozenset(),
    ) -> None:
        add_map = self._as_entries(add)
        remove_map = self._as_entries(remove)
        # Prioritní symboly první, zbytek abecedně (stabilní pořadí)
        ordered_add = sorted(add_map, key=lambda symbol: (symbol not in self._priority, symbol))
        entries_add = [
            {"type": event, "symbol": symbol}
            for symbol in ordered_add
            for event in sorted(add_map[symbol])
        ]
        entries_remove = [
            {"type": event, "symbol": symbol}
            for symbol in sorted(remove_map)
            for event in sorted(remove_map[symbol])
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
                # too high` v produkci při ~4 700 symbolech. Rozestup je
                # adaptivní (#863): po rate limitu se zdvojí.
                await asyncio.sleep(self._pause_s)
                # Dlouhý (zpomalený) resubscribe nesmí prošvihnout keepalive —
                # se stropem 2 s na dávku trvá plná množina přes minutu (#863)
                if time.monotonic() - self._last_keepalive > KEEPALIVE_INTERVAL_S:
                    await self._send({"type": "KEEPALIVE", "channel": 0})
                    self._last_keepalive = time.monotonic()

    async def _connect_and_read(self, stop: asyncio.Event) -> None:
        url, token = await self._token_source()
        async with websockets.connect(
            url,
            max_size=2**24,
            ping_interval=KEEPALIVE_INTERVAL_S,
            ping_timeout=PING_TIMEOUT_S,
        ) as ws:
            self._ws = ws
            try:
                # Handshake je sdílený s backfillem svíček (#617) — jedna
                # implementace, ať se dvě kopie nerozejdou
                await handshake(ws, token, {name: EVENT_FIELDS[name] for name in self._events})
                async with self._lock:
                    await self._send_subscription(add=dict(self._subs))
                    # Dokončení musí být v logu vidět (#916): smyčka smrti se
                    # poznala až z nepřímých příznaků, protože nedoběhnutá
                    # subskripce nikde nechyběla
                    logger.info(
                        "DXLink subskripce kompletní: %d symbolů (à %.2f s)",
                        len(self._symbols),
                        self._pause_s,
                    )

                self._last_keepalive = time.monotonic()
                while not stop.is_set():
                    if time.monotonic() - self._last_keepalive > KEEPALIVE_INTERVAL_S:
                        await self._send({"type": "KEEPALIVE", "channel": 0})
                        self._last_keepalive = time.monotonic()
                    # Samoléčení po rate limitu (#863): po zklidnění poslat
                    # celou cílovou množinu znovu (idempotentní add) — server
                    # nehlásí, které dávky zahodil, a odmítnuté symboly by
                    # jinak mlčely až do reconnectu. Pomalejší `_pause_s`
                    # zajistí, že opakování konverguje.
                    if self._heal_due(time.monotonic()):
                        self._rate_limit_ts = None
                        async with self._lock:
                            full = set(self._subs)
                            targets = full
                            if self._heal_targets is not None:
                                silent = self._heal_targets(full) & full
                                # Cíleně jen když mlčí menšina — mlčící většina
                                # znamená výpadek, tam patří plný resubscribe
                                if len(silent) < len(full) / 2:
                                    targets = silent
                            self.heals += 1
                            if targets:
                                logger.warning(
                                    "DXLink rate limit: heal %d/%d symbolů (à %.2f s)",
                                    len(targets),
                                    len(full),
                                    self._pause_s,
                                )
                                await self._send_subscription(
                                    add={symbol: self._subs[symbol] for symbol in targets}
                                )
                            else:
                                logger.info(
                                    "DXLink rate limit: vše dodává, heal netřeba (%d symbolů)",
                                    len(full),
                                )
                    # Relaxace rozestupu (#936): po klidu bez rate limitu se
                    # pauza vrací k základu — jinak by ranní špička zpomalila
                    # subskripce na celý den
                    if (
                        self._pause_s > SUBSCRIPTION_PAUSE_S
                        and self._rate_limit_ts is None
                        and time.monotonic() - self._last_rate_limit_seen > PAUSE_RELAX_AFTER_S
                    ):
                        self._pause_s = max(SUBSCRIPTION_PAUSE_S, self._pause_s / 2)
                        self._last_rate_limit_seen = time.monotonic()
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
                        self._note_server_error(message)
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
