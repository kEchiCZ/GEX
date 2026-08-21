"""Alpaca news WebSocket — real-time headlines pushem (#387).

Benzinga zprávy přes `wss://stream.data.alpaca.markets/v1beta1/news`, zdarma
i s paper účtem. Na rozdíl od ostatních zdrojů nejde o polling (`Collector`
protokol s `fetch()`), ale o trvalé spojení s pushem — proto vlastní asyncio
task s reconnectem, ne položka v `CollectorRunner`.

Chyby spojení nikdy neshodí engine (kap. 10): výpadek → exponenciální backoff
a reconnect; překryv s RSS řeší stávající dedup (`dedup_hash` titulek + čas).
"""

import asyncio
import contextlib
import datetime as dt
import json
import logging
from collections.abc import Sequence
from typing import Any, Protocol

from gexlens_engine.compute.newstext import clip_body, strip_html
from gexlens_news.model import NewsEvent

logger = logging.getLogger(__name__)

ALPACA_NEWS_WS_URL = "wss://stream.data.alpaca.markets/v1beta1/news"
SOURCE_NAME = "alpaca"

# Backoff reconnectu: start 5 s, zdvojnásobování do stropu
RECONNECT_BASE_S = 5.0
RECONNECT_MAX_S = 60.0


class EventWriter(Protocol):
    """Zápis normalizovaných eventů (DedupingWriter.write je sync)."""

    def write(self, events: Sequence[NewsEvent]) -> int: ...


def normalize_message(payload: dict[str, Any], now: dt.datetime) -> NewsEvent | None:
    """Zpráva `{"T": "n", ...}` → NewsEvent; jiné typy (success, error) → None."""
    if payload.get("T") != "n":
        return None
    headline = str(payload.get("headline") or "").strip()
    if not headline:
        return None
    created = payload.get("created_at")
    try:
        ts_event = dt.datetime.fromisoformat(str(created)) if created else now
    except ValueError:
        ts_event = now
    if ts_event.tzinfo is None:
        ts_event = ts_event.replace(tzinfo=dt.UTC)
    summary = str(payload.get("summary") or "").strip() or None
    # Plné znění (#743): Alpaca ho posílá v `content` jako HTML a dosud se
    # zahazovalo — model tak měl k dispozici jen ~50 znaků titulku. Značky se
    # odstraňují hned, do DB nepatří (a do promptu ani do rysů už vůbec).
    # Ořez na BODY_MAX_CHARS (#744): průměrný článek má ~3,5 kB a dvouletý
    # backfill by tak zabral ~177 MB; model čte stejně jen lead.
    body = clip_body(strip_html(str(payload.get("content") or ""))) or None
    symbols = [str(symbol) for symbol in payload.get("symbols") or []]
    uid = payload.get("id")
    return NewsEvent(
        ts_event=ts_event,
        ts_ingested=now,
        source=SOURCE_NAME,
        kind="headline",
        title=headline,
        summary=summary,
        body=body,
        source_uid=str(uid) if uid is not None else None,
        symbols=symbols,
        raw={"benzinga_url": payload.get("url"), "author": payload.get("author")},
    )


class AlpacaNewsStream:
    """Trvalé WS spojení: auth → subscribe news ["*"] → push eventů do writeru."""

    def __init__(
        self,
        key_id: str,
        secret: str,
        writer: EventWriter,
        *,
        url: str = ALPACA_NEWS_WS_URL,
    ) -> None:
        self._key_id = key_id
        self._secret = secret
        self._writer = writer
        self._url = url

    async def run(self, stop: asyncio.Event) -> None:
        """Smyčka spojení s backoffem; končí až se stop eventem."""
        backoff = RECONNECT_BASE_S
        while not stop.is_set():
            try:
                await self._session(stop)
                backoff = RECONNECT_BASE_S  # čisté odpojení → rychlý reconnect
            except Exception as exc:
                # repr + typ: str() je u ConnectionClosed/IncompleteReadError
                # a timeoutů prázdný a v logu zbylo „spadlo ()" (#776)
                logger.warning(
                    "Alpaca WS spadlo (%s: %r) — reconnect za %.0f s",
                    type(exc).__name__,
                    exc,
                    backoff,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(RECONNECT_MAX_S, backoff * 2)

    async def _session(self, stop: asyncio.Event) -> None:
        import websockets

        async with websockets.connect(self._url, ping_interval=20) as ws:
            await ws.send(
                json.dumps({"action": "auth", "key": self._key_id, "secret": self._secret})
            )  # noqa: E501
            await ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
            logger.info("Alpaca news WS připojeno (subscribe news: *)")
            while not stop.is_set():
                raw = await asyncio.wait_for(ws.recv(), timeout=90)
                await self._handle(raw)

    async def _handle(self, raw: str | bytes) -> None:
        try:
            messages = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Alpaca WS: nečitelný rámec (%d B)", len(raw))
            return
        if not isinstance(messages, list):
            messages = [messages]
        now = dt.datetime.now(dt.UTC)
        events = []
        for message in messages:
            if isinstance(message, dict) and message.get("T") == "error":
                # Auth/subscribe chyba je fatální pro session — reconnect ji zopakuje,
                # ale musí být vidět v logu (typicky špatné klíče)
                raise RuntimeError(f"Alpaca WS error: {message.get('msg')}")
            if isinstance(message, dict):
                event = normalize_message(message, now)
                if event is not None:
                    events.append(event)
        if events:
            written = await asyncio.to_thread(self._writer.write, events)
            if written:
                logger.info("Alpaca WS: +%d headlines", written)
