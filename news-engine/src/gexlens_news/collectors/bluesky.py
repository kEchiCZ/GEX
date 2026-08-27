"""Bluesky Jetstream (#578, zdroj vybraný uživatelem 27. 8. 2026).

Kanál přímé komunikace osob: finanční novináři a analytici se na síť
přesunuli a platforma zavedla cashtags — přesně ta kategorie kurzotvorných
zpráv, která do agenturní pásky dorazí až s odstupem (latence z #386/#387).

Zdroj je veřejný **Jetstream** (AT Protocol): WebSocket bez autentizace se
VŠEMI posty sítě; oficiální searchPosts API neautentizované requesty odmítá
(403, změřeno 27. 8.), firehose ne. Filtruje se klientsky:

- cashtags a makro klíčová slova relevantní pro ES/NQ (viz vzory níže),
- volitelně kurátorovaní autoři (handle/DID) — u nich se bere každý post,
- jen angličtina (langs), pojistka proti povodni MAX_PER_MINUTE.

Sentiment hodnotí NÁŠ klasifikátor (rozhodnutí uživatele: žádný FinBERT).
Tier v registru zdrojů: testovací — váhu si musí teprve vyměřit (audit B).
"""

import asyncio
import contextlib
import datetime as dt
import json
import logging
import re
from collections.abc import Iterable, Sequence
from typing import Any, Protocol

from gexlens_news.model import NewsEvent

logger = logging.getLogger(__name__)

SOURCE_NAME = "bluesky"
JETSTREAM_URL = (
    "wss://jetstream2.us-east.bsky.network/subscribe?wantedCollections=app.bsky.feed.post"
)
RECONNECT_BASE_S = 2.0
RECONNECT_MAX_S = 60.0
#: Pojistka proti povodni (referenční varování: „příliš zdrojů zblbne systémy")
MAX_PER_MINUTE = 30
#: Ořez titulku — post umí 300 grafémů, DB titulek je zkrácené čtení
TITLE_MAX_CHARS = 240

#: Kurzotvorné cashtags pro ES/NQ kontext: indexy, proxy ETF a mega caps
CASHTAG_PATTERN = re.compile(
    r"\$(SPY|QQQ|SPX|NDX|VIX|IWM|DIA|ES|NQ|NVDA|AAPL|MSFT|TSLA|AMZN|GOOGL?|META|AVGO)\b",
    re.IGNORECASE,
)
#: Makro slovník — úzký schválně: široké vzory by tahaly balast (šum > signál)
KEYWORD_PATTERN = re.compile(
    r"\b(FOMC|federal reserve|rate (cut|hike)s?|CPI|PCE inflation|nonfarm payrolls?"
    r"|jobs report|Powell|S&P ?500|Nasdaq ?100|treasury yields?|breaking:)\b",
    re.IGNORECASE,
)


class EventWriter(Protocol):
    """Stejný kontrakt jako DedupingWriter.write."""

    def write(self, events: Sequence[NewsEvent]) -> int: ...


def matches(text: str, author: str, curated: frozenset[str]) -> bool:
    """Kurzotvorný post? Kurátorovaný autor bere vše, jinak cashtag/klíčové slovo."""
    if author in curated:
        return True
    return bool(CASHTAG_PATTERN.search(text) or KEYWORD_PATTERN.search(text))


def normalize_post(message: dict[str, Any], now: dt.datetime) -> NewsEvent | None:
    """Jetstream commit → NewsEvent; jiné zprávy (identity, account, delete) → None."""
    if message.get("kind") != "commit":
        return None
    commit = message.get("commit") or {}
    if commit.get("operation") != "create" or commit.get("collection") != "app.bsky.feed.post":
        return None
    record = commit.get("record") or {}
    text = str(record.get("text") or "").strip()
    if not text:
        return None
    langs = record.get("langs") or []
    if langs and "en" not in langs:
        return None
    created = record.get("createdAt")
    try:
        ts_event = dt.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        ts_event = now
    if ts_event.tzinfo is None:
        ts_event = ts_event.replace(tzinfo=dt.UTC)
    # Budoucí/rozjeté hodiny klientů: event nesmí předběhnout ingest
    ts_event = min(ts_event, now)
    did = str(message.get("did") or "")
    rkey = str(commit.get("rkey") or "")
    title = text[:TITLE_MAX_CHARS] + ("…" if len(text) > TITLE_MAX_CHARS else "")
    return NewsEvent(
        ts_event=ts_event,
        ts_ingested=now,
        source=SOURCE_NAME,
        kind="social",
        title=title,
        summary=None,
        body=text if len(text) > TITLE_MAX_CHARS else None,
        source_uid=f"at://{did}/app.bsky.feed.post/{rkey}" if did and rkey else None,
        symbols=[],
        raw={"did": did, "langs": langs},
    )


class BlueskyStream:
    """Trvalé Jetstream spojení s reconnectem; filtrované posty do writeru.

    Vzor AlpacaNewsStream (#387): vlastní task vedle CollectorRunneru,
    protože firehose není polling zdroj.
    """

    def __init__(
        self,
        writer: EventWriter,
        *,
        url: str = JETSTREAM_URL,
        curated_authors: Iterable[str] = (),
    ) -> None:
        self._writer = writer
        self._url = url
        self._curated = frozenset(curated_authors)
        # Hot-reload (#578): bluesky_loop volá set_curated při změně seznamu
        #: Diagnostika: přijaté / prošlé filtrem / zahozené pojistkou
        self.seen = 0
        self.matched = 0
        self.flood_dropped = 0
        self._minute_window: dt.datetime | None = None
        self._minute_count = 0

    def set_curated(self, curated: frozenset[str]) -> None:
        """Vymění kurátory za běhu — atomická náhrada, stream nepadá."""
        if curated != self._curated:
            self._curated = curated
            logger.info("Bluesky: kurátoři aktualizováni (%d DID)", len(curated))

    async def run(self, stop: asyncio.Event) -> None:
        backoff = RECONNECT_BASE_S
        while not stop.is_set():
            try:
                await self._session(stop)
                backoff = RECONNECT_BASE_S
            except Exception as exc:
                logger.warning(
                    "Bluesky Jetstream spadl (%s: %r) — reconnect za %.0f s",
                    type(exc).__name__,
                    exc,
                    backoff,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(RECONNECT_MAX_S, backoff * 2)

    async def _session(self, stop: asyncio.Event) -> None:
        import websockets

        async with websockets.connect(self._url, max_size=2**22, ping_interval=20) as ws:
            logger.info("Bluesky Jetstream připojen (%s)", self._url.split("?")[0])
            while not stop.is_set():
                raw = await asyncio.wait_for(ws.recv(), timeout=60.0)
                self._handle(raw)

    def _handle(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return
        self.seen += 1
        now = dt.datetime.now(dt.UTC)
        event = normalize_post(message, now)
        if event is None:
            return
        author = str(message.get("did") or "")
        if not matches(event.title + (event.body or ""), author, self._curated):
            return
        # Pojistka proti povodni: nad MAX_PER_MINUTE se počítá a zahazuje
        minute = now.replace(second=0, microsecond=0)
        if self._minute_window != minute:
            self._minute_window = minute
            self._minute_count = 0
        if self._minute_count >= MAX_PER_MINUTE:
            self.flood_dropped += 1
            return
        self._minute_count += 1
        self.matched += 1
        try:
            self._writer.write([event])
        except Exception:
            logger.exception("Bluesky: zápis eventu selhal — stream jede dál")
