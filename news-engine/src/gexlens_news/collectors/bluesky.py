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

Výpadek spojení (#1509): každá zpráva Jetstreamu nese `time_us` — čas příjmu
v Jetstreamu v µs unixového času, na jedné instanci striktně rostoucí. Stream
si ho pamatuje a reconnect posílá jako `cursor`; Jetstream přehraje zprávy od
cursoru včetně a přejde na živý proud. Cursor žije jen v paměti procesu:
restart news-enginu začíná živým proudem a posty z doby restartu chybí.
"""

import asyncio
import contextlib
import dataclasses
import datetime as dt
import json
import logging
import re
import time
from collections.abc import Iterable, Sequence
from typing import Any, Protocol

from gexlens_engine.compute.newstext import unescape_entities
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
    # Post je prostý text, ale boti přeposílající RSS píšou entity doslova
    # (`S&amp;P 500`); dekóduje se před ořezem titulku, ať se nepřeřízne.
    # Odkazy s `&region=US` zůstanou, viz `unescape_entities` (#1500)
    text = unescape_entities(str(record.get("text") or "")).strip()
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


def _time_us(message: dict[str, Any]) -> int | None:
    """`time_us` zprávy Jetstreamu (commit, identity i account); chybí-li, None."""
    value = message.get("time_us")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


@dataclasses.dataclass
class _Replay:
    """Přehrání po reconnectu s cursorem (#1509): zprávy od cursoru do připojení."""

    cursor_us: int
    connected_us: int
    received: int = 0
    matched: int = 0
    written: int = 0

    @property
    def gap_s(self) -> float:
        """Délka výpadku: poslední přijatá zpráva → okamžik nového připojení."""
        return (self.connected_us - self.cursor_us) / 1_000_000

    def log(self, state: str) -> None:
        logger.info(
            "Bluesky: přehrání %s — výpadek %.0f s, přehráno %d zpráv, "
            "filtrem prošlo %d, zapsáno %d",
            state,
            self.gap_s,
            self.received,
            self.matched,
            self.written,
        )


class BlueskyStream:
    """Trvalé Jetstream spojení s reconnectem; filtrované posty do writeru.

    Vzor AlpacaNewsStream (#387): vlastní task vedle CollectorRunneru,
    protože firehose není polling zdroj.

    Reconnect s cursorem (#1509): přehrání je inkluzivní, zpráva s `time_us`
    rovným cursoru tedy přijde podruhé. Zprávy s `time_us <=` poslední přijaté
    zahodí už stream, ne až writer. Uvnitř spojení pravidlo nic nového nezahodí:
    Jetstream posílá každému odběrateli `time_us` striktně rostoucí (starší
    zprávu sám přeskočí). `DedupingWriter` slučuje stories podle
    normalizovaného titulku v okně 6 h; je to sloučení zpráv napříč zdroji,
    ne záruka jednoho doručení. Duplicita by navíc ukrojila z povodňového
    stropu své minuty a zkreslila počítadla i log přehrání. Identity
    a account zprávy, které posouvají cursor, writer vůbec nevidí.
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
        #: `time_us` poslední přijaté zprávy = cursor příštího připojení (#1509)
        self._cursor: int | None = None
        #: Doručila aktuální session aspoň jeden rámec? (reset backoffu)
        self._session_delivered = False
        self._replay: _Replay | None = None

    def set_curated(self, curated: frozenset[str]) -> None:
        """Vymění kurátory za běhu — atomická náhrada, stream nepadá."""
        if curated != self._curated:
            self._curated = curated
            logger.info("Bluesky: kurátoři aktualizováni (%d DID)", len(curated))

    async def run(self, stop: asyncio.Event) -> None:
        """Smyčka spojení s backoffem; končí až se stop eventem.

        Backoff roste jen opakovaným selháním připojení (#1509). Spojení, které
        doručilo aspoň jeden rámec, fungovalo, a první reconnect po jeho pádu
        jde za RECONNECT_BASE_S. Úspěch = rámec, ne handshake: server, který
        spojení přijme a hned zavře, nesmí roztočit reconnect à 2 s.
        """
        backoff = RECONNECT_BASE_S
        while not stop.is_set():
            self._session_delivered = False
            try:
                await self._session(stop)
                backoff = RECONNECT_BASE_S
            except Exception as exc:
                if self._session_delivered:
                    backoff = RECONNECT_BASE_S
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

        url = self._url
        if self._cursor is not None:
            # Bez záporného bufferu: ten README Jetstreamu radí při přepnutí na
            # jinou instanci (vlastní hodiny `time_us`), my se vracíme na tutéž
            url += ("&" if "?" in url else "?") + f"cursor={self._cursor}"
        async with websockets.connect(url, max_size=2**22, ping_interval=20) as ws:
            host = self._url.split("?")[0]
            if self._cursor is None:
                logger.info("Bluesky Jetstream připojen (%s)", host)
            else:
                # Okamžik připojení z hodin news-enginu, `time_us` z hodin
                # Jetstreamu — rozjeté hodiny posunou jen hranici v logu
                self._replay = _Replay(self._cursor, time.time_ns() // 1_000)
                logger.info(
                    "Bluesky Jetstream připojen (%s) s cursorem — výpadek %.0f s, přehrává se",
                    host,
                    self._replay.gap_s,
                )
            try:
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=60.0)
                    self._session_delivered = True
                    self._handle(raw)
            finally:
                if self._replay is not None:
                    self._replay.log("nedokončeno (pád nebo stop)")
                    self._replay = None

    def _handle(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return
        time_us = _time_us(message)
        if time_us is not None:
            if self._cursor is not None and time_us <= self._cursor:
                return  # hranice cursoru: přehrání je inkluzivní (viz docstring třídy)
            # Cursor se posune před zpracováním: zprávu, na které zpracování
            # spadne, reconnect znovu nepřehraje (žádná smyčka pádů)
            self._cursor = time_us
        replay = self._replay
        if replay is not None and time_us is not None and time_us >= replay.connected_us:
            # První zpráva z doby po připojení = živý proud dohnán
            replay.log("dohnáno")
            self._replay = replay = None
        self.seen += 1
        written = self._ingest(message, time_us)
        if replay is not None:
            replay.received += 1
            if written is not None:
                replay.matched += 1
                replay.written += written

    def _ingest(self, message: dict[str, Any], time_us: int | None) -> int | None:
        """Filtr, pojistka a zápis; None = post neprošel, jinak počet zapsaných."""
        now = dt.datetime.now(dt.UTC)
        event = normalize_post(message, now)
        if event is None:
            return None
        author = str(message.get("did") or "")
        if not matches(event.title + (event.body or ""), author, self._curated):
            return None
        if author in self._curated:
            # Příznak kurátora (#1291): jen kurátorovaný autor dělá sociální
            # zprávu významnou; seznam kurátorů zná jen stream za běhu
            event = dataclasses.replace(event, raw={**event.raw, "curated": True})
        # Pojistka proti povodni: nad MAX_PER_MINUTE se počítá a zahazuje.
        # Minuta podle času zprávy v Jetstreamu (#1509): přehrání po reconnectu
        # se rozloží do původních minut, živý proud má `time_us` ≈ teď
        at = now if time_us is None else dt.datetime.fromtimestamp(time_us // 1_000_000, dt.UTC)
        minute = at.replace(second=0, microsecond=0)
        if self._minute_window != minute:
            self._minute_window = minute
            self._minute_count = 0
        if self._minute_count >= MAX_PER_MINUTE:
            self.flood_dropped += 1
            return None
        self._minute_count += 1
        self.matched += 1
        try:
            return self._writer.write([event])
        except Exception:
            logger.exception("Bluesky: zápis eventu selhal — stream jede dál")
            return 0
