"""WebSocket /ws/live (SPEC kap. 6): pub/sub hub s kanály, delta updaty a backpressure.

Kanály: price.{sym}, snapshot.{sym}.{expiry}, levels.*, flow.*, status, news,
alerts. Klient subskribuje zprávou {"action": "subscribe", "channels": [...]};
server pushuje. Engine publikuje přes LiveHub.publish.

Backpressure: každý klient má frontu s pevnou kapacitou; při zaplnění se
zahazují nejstarší framy (pomalý klient nikdy neblokuje publish ani server).
"""

import asyncio
import contextlib
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

Message = dict[str, object]

# Stropy proti vyčerpání zdrojů (#542 H2). `publish` iteruje vzory KAŽDÉHO
# subskribenta, takže neomezená množina kanálů je CPU DoS v event loopu —
# degraduje doručování všem ostatním, nejen útočníkovi.
MAX_SUBSCRIBERS = 64
MAX_CHANNELS_PER_SUBSCRIBER = 256
MAX_CHANNEL_LENGTH = 128
# Frontend odebírá kanály tvaru `levels.ES.20260807` nebo `price.*`.
# Hvězdička smí být JEN posledním segmentem — přesně ten tvar umí
# `channel_matches` níž (prefixová shoda). `*` samotná ani `a.*.b` ne.
# Původní `[A-Za-z0-9_.\-]+` hvězdičku nepouštělo vůbec, čímž od #542 tiše
# padal celý úvodní rámec frontendu (`status`, `alerts`, `setups.*`) — #948.
CHANNEL_RE = re.compile(r"^[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)*(\.\*)?$")


class TooManySubscribers(Exception):
    """Vyčerpaný strop souběžných WS spojení."""


class TooManyChannels(Exception):
    """Subskribent překročil strop kanálů."""


@dataclass(frozen=True)
class ParsedChannels:
    """Rozebraný subskripční rámec: co se smí subskribovat a co se zahodilo."""

    valid: list[str]
    rejected: list[str]


def parse_channels(raw: object) -> ParsedChannels:
    """Rozdělí kanály z klientské zprávy na platné a odmítnuté.

    Bez kontroly typu se řetězec `"abc"` iteroval po znacích (subskripce
    kanálů `a`, `b`, `c`) a číslo shodilo spojení neodchyceným TypeError.

    Vadná POLOŽKA rámec neshazuje (#948): dřív se při první neplatné vyhodila
    výjimka a handler zahodil celý rámec, takže kvůli `setups.*` neprošel ani
    `status`, ani `alerts` — a klient o tom nevěděl. Neplatné se teď jen
    vynechají a vrátí v `rejected`, ať je vidět, co se nesubskribovalo.
    Bezpečnostní vlastnost zůstává: odmítnutý kanál se NEsubskribuje.

    Vadný TVAR celé zprávy (není seznam, přes strop) výjimku vyhazuje dál —
    tam není co zachraňovat.
    """
    if not isinstance(raw, list):
        raise ValueError("Pole channels musí být seznam řetězců")
    if len(raw) > MAX_CHANNELS_PER_SUBSCRIBER:
        raise ValueError(f"Najednou lze poslat nejvýš {MAX_CHANNELS_PER_SUBSCRIBER} kanálů")
    valid: list[str] = []
    rejected: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            rejected.append(repr(item)[:MAX_CHANNEL_LENGTH])
            continue
        if len(item) > MAX_CHANNEL_LENGTH or not CHANNEL_RE.match(item):
            rejected.append(item[:MAX_CHANNEL_LENGTH])
            continue
        valid.append(item)
    return ParsedChannels(valid=valid, rejected=rejected)


@dataclass
class _Subscriber:
    queue: asyncio.Queue[Message]
    channels: set[str] = field(default_factory=set)


def channel_matches(patterns: set[str], channel: str) -> bool:
    """Přesná shoda nebo trailing wildcard (`levels.*` pokrývá `levels.ES…`)."""
    if channel in patterns:
        return True
    return any(pattern.endswith(".*") and channel.startswith(pattern[:-1]) for pattern in patterns)


class LiveHub:
    """Pub/sub rozcestník mezi enginem (publish) a WebSocket klienty (fronty)."""

    def __init__(self, queue_size: int = 100, max_subscribers: int = MAX_SUBSCRIBERS) -> None:
        self._queue_size = queue_size
        self._max_subscribers = max_subscribers
        self._subscribers: dict[int, _Subscriber] = {}
        self._next_id = 0

    def register(self) -> tuple[int, asyncio.Queue[Message]]:
        if len(self._subscribers) >= self._max_subscribers:
            raise TooManySubscribers(f"Strop {self._max_subscribers} souběžných WS spojení")
        self._next_id += 1
        subscriber = _Subscriber(queue=asyncio.Queue(maxsize=self._queue_size))
        self._subscribers[self._next_id] = subscriber
        return self._next_id, subscriber.queue

    def unregister(self, subscriber_id: int) -> None:
        self._subscribers.pop(subscriber_id, None)

    def subscribe(self, subscriber_id: int, channels: Iterable[str]) -> set[str]:
        subscriber = self._subscribers[subscriber_id]
        merged = subscriber.channels | set(channels)
        if len(merged) > MAX_CHANNELS_PER_SUBSCRIBER:
            raise TooManyChannels(
                f"Strop {MAX_CHANNELS_PER_SUBSCRIBER} kanálů na spojení "
                f"(zbývá {MAX_CHANNELS_PER_SUBSCRIBER - len(subscriber.channels)})"
            )
        subscriber.channels = merged
        return set(subscriber.channels)

    def unsubscribe(self, subscriber_id: int, channels: Iterable[str]) -> set[str]:
        subscriber = self._subscribers[subscriber_id]
        subscriber.channels.difference_update(channels)
        return set(subscriber.channels)

    def publish(self, channel: str, payload: Message) -> int:
        """Rozešle zprávu subskribentům kanálu; vrací počet doručení.

        Nikdy neblokuje: plná fronta pomalého klienta zahodí nejstarší frame
        (SPEC: backpressure — drop starých framů).
        """
        message: Message = {"channel": channel, "data": payload}
        delivered = 0
        for subscriber in list(self._subscribers.values()):
            if not channel_matches(subscriber.channels, channel):
                continue
            try:
                subscriber.queue.put_nowait(message)
            except asyncio.QueueFull:
                # Zahoď nejstarší frame; QueueEmpty = závod s konzumentem, fronta se uvolnila
                with contextlib.suppress(asyncio.QueueEmpty):
                    subscriber.queue.get_nowait()
                subscriber.queue.put_nowait(message)
            delivered += 1
        return delivered


class SnapshotDeltaTracker:
    """Delta updaty heatmapy: publikují se jen změněné buňky (SPEC kap. 6).

    Engine po každé minutě předá kompletní buňky; tracker vrátí jen ty,
    které se od minula změnily (první minuta = všechny).
    """

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], dict[tuple[float, str], tuple[object, ...]]] = {}

    def delta(
        self, symbol: str, expiry: str, cells: Iterable[dict[str, object]]
    ) -> list[dict[str, object]]:
        key = (symbol, expiry)
        previous = self._last.get(key, {})
        current: dict[tuple[float, str], tuple[object, ...]] = {}
        changed: list[dict[str, object]] = []
        for cell in cells:
            cell_key = (float(cell["strike"]), str(cell["right"]))  # type: ignore[arg-type]
            fingerprint = tuple(
                value for name, value in sorted(cell.items()) if name not in ("strike", "right")
            )
            current[cell_key] = fingerprint
            if previous.get(cell_key) != fingerprint:
                changed.append(cell)
        self._last[key] = current
        return changed
