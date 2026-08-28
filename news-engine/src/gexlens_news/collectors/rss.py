"""Obecný RSS/Atom collector — sdílený Tier A (Fed) i Tier B (CNBC, MarketWatch…).

Parsuje se stdlib `xml.etree`, ne externí knihovnou: potřebujeme čtyři pole
(titulek, čas, odkaz, popis) a další závislost by přinesla víc rizika než užitku.
Nečitelná položka se přeskočí, celá dávka kvůli ní nepadá (SPEC 3.2).

Conditional GET je tu podstatný — díky 304 může feed jet à 60 s (SPEC kap. 1).

**Proč ne `defusedxml` (#552 L4).** Feed je nedůvěryhodný vstup, ale oba klasické
XML útoky jsou tu pokryté i bez další závislosti:

* **XXE** — Python expat externí entity neresolvuje, `ElementTree` navíc žádné
  rozhraní pro jejich načtení nenabízí.
* **billion laughs** — mitigované v expatu od 2.4; produkční image má **2.7.3**
  (ověřeno 17. 8. 2026 v běžícím kontejneru). Aby tenhle předpoklad nezůstal
  jen v komentáři, hlídá spodní hranici test `test_expat_umi_branit_amplifikaci`
  — po výměně base image spadne CI, ne provoz.
* **velký vnořený XML** jako DoS vektor stál na tom, že se stahovalo tělo bez
  omezení; to řeší strop `MAX_RESPONSE_BYTES` v `http.py` (M4).

Rozhodnutí se tedy neopírá o „riziko je nízké", ale o tři konkrétní zábrany.
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Sequence
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from httpx import HTTPStatusError

from gexlens_news.collectors import CollectorClock, utc_now
from gexlens_news.http import Fetcher, Response
from gexlens_news.model import NewsEvent, RawItem

logger = logging.getLogger(__name__)

_ATOM = "{http://www.w3.org/2005/Atom}"
# Maximální délka `summary` (SPEC 2.1) — do DB jde stručný text, ne celý článek
SUMMARY_LIMIT = 500
# L3 (#552): titulek jde do Gemini promptu a `news_events.title` je Text bez
# limitu — megabajtový titulek by spálil tokenový rozpočet. Reálné titulky mají
# desítky znaků, 500 je řádová rezerva.
TITLE_LIMIT = 500
# L2 (#552): dedup chrání proti opakování, ne proti tisícům UNIKÁTNÍCH titulků.
# Bez stropu zaplaví jeden nepřátelský feed `news_events`, WS push i frontu
# Gemini. Největší reálný feed má desítky položek na dávku.
MAX_ITEMS_PER_FEED = 200


def parse_feed_time(raw: str | None) -> dt.datetime | None:
    """Čas položky: RFC 822 (RSS) i ISO 8601 (Atom). Naivní čas bereme jako UTC."""
    if not raw:
        return None
    text = raw.strip()
    for parser in (parsedate_to_datetime, dt.datetime.fromisoformat):
        try:
            parsed = parser(text)
        except (TypeError, ValueError):
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    logger.debug("Nečitelný čas položky %r", raw)
    return None


def _text(element: ElementTree.Element, *names: str) -> str | None:
    for name in names:
        found = element.find(name)
        if found is not None:
            if found.text and found.text.strip():
                return found.text.strip()
            # Atom <link href="..."/> nese hodnotu v atributu
            if href := found.get("href"):
                return href
    return None


def parse_items(xml: str) -> list[dict[str, str | None]]:
    """Položky feedu jako slovníky; nevalidní XML vyhodí (runner to ustojí)."""
    root = ElementTree.fromstring(xml)
    entries = root.iter("item") if root.iter("item") else []
    items = [
        {
            "title": _text(entry, "title"),
            "link": _text(entry, "link"),
            "published": _text(entry, "pubDate", "published", "updated", "date"),
            "summary": _text(entry, "description", "summary"),
            "guid": _text(entry, "guid", "id"),
        }
        for entry in entries
    ]
    if items:
        return items
    # Atom
    return [
        {
            "title": _text(entry, f"{_ATOM}title"),
            "link": _text(entry, f"{_ATOM}link"),
            "published": _text(entry, f"{_ATOM}published", f"{_ATOM}updated"),
            "summary": _text(entry, f"{_ATOM}summary", f"{_ATOM}content"),
            "guid": _text(entry, f"{_ATOM}id"),
        }
        for entry in root.iter(f"{_ATOM}entry")
    ]


def _describe(error: Exception) -> str:
    """Chyba do souhrnu: u HTTP i status kód (#941).

    Samotné `HTTPStatusError` neřeklo, jestli jde o 429 (rate limit), 404
    (zrušený feed) nebo 500 — a diagnóza pak stála reprodukci v kontejneru.
    """
    if isinstance(error, HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    return type(error).__name__


class RssCollector:
    """Jeden nebo více RSS/Atom feedů pod společným jménem zdroje."""

    def __init__(
        self,
        name: str,
        urls: Sequence[str],
        fetcher: Fetcher,
        *,
        interval_s: float = 60.0,
        kind: str = "headline",
        category: str | None = None,
        importance: int | None = None,
        symbols: Sequence[str] = (),
        inter_fetch_delay_s: float = 0.0,
        round_robin: bool = False,
        clock: CollectorClock = utc_now,
    ) -> None:
        self._name = name
        self._urls = list(urls)
        self._fetcher = fetcher
        self._interval_s = interval_s
        self._kind = kind
        self._category = category
        self._importance = importance
        self._symbols = list(symbols)
        #: Rozestup mezi feedy (#922) — u zdrojů, které nesnesou dávku za sebou
        self._inter_fetch_delay_s = inter_fetch_delay_s
        #: Round robin (#941): jeden feed za cyklus místo všech naráz.
        #: Reddit limituje anonymní přístup per IP a měření 29. 8. ukázalo, že
        #: potřebuje ~150 s klidu mezi požadavky — při kadenci 300 s tedy projde
        #: právě jeden. Každý subreddit se tak stáhne à (interval × počet feedů),
        #: což pro crowd sentiment bohatě stačí.
        self._round_robin = round_robin
        self._cursor = 0
        self._clock = clock

    @property
    def name(self) -> str:
        return self._name

    @property
    def interval_s(self) -> float:
        return self._interval_s

    async def _fetch_with_retry(self, url: str) -> Response:
        """Jeden pokus navíc při 429 (#941).

        Reddit rate limituje anonymní přístup per IP; občasné odmítnutí je
        provozní realita, ne porucha feedu. Druhý pokus po rozestupu zachrání
        cyklus místo toho, aby zdroj hlásil degradaci.
        """
        try:
            return await self._fetcher.get(url)
        except HTTPStatusError as error:
            if error.response.status_code != 429 or self._inter_fetch_delay_s <= 0:
                raise
            logger.info(
                "Feed %s vrátil 429 — opakuji za %.0f s (#941)", url, self._inter_fetch_delay_s
            )
            await asyncio.sleep(self._inter_fetch_delay_s)
            return await self._fetcher.get(url)

    def _due_urls(self) -> list[str]:
        """Feedy k stažení v tomto cyklu (round robin → právě jeden)."""
        if not self._round_robin or not self._urls:
            return list(self._urls)
        url = self._urls[self._cursor % len(self._urls)]
        self._cursor += 1
        return [url]

    async def fetch(self) -> Sequence[RawItem]:
        now = self._clock()
        items: list[RawItem] = []
        errors: list[str] = []
        for index, url in enumerate(self._due_urls()):
            if index > 0 and self._inter_fetch_delay_s > 0:
                await asyncio.sleep(self._inter_fetch_delay_s)
            try:
                response = await self._fetch_with_retry(url)
                if response.not_modified:
                    continue  # 304 — feed se nezměnil, nic k práci
                entries = parse_items(response.text)
                if len(entries) > MAX_ITEMS_PER_FEED:
                    # Ořez, ne zahození: zdroj může legitimně poslat víc po
                    # výpadku. Novější položky jsou ve feedu první.
                    logger.warning(
                        "Feed %s poslal %d položek, beru prvních %d (#552 L2)",
                        url,
                        len(entries),
                        MAX_ITEMS_PER_FEED,
                    )
                    entries = entries[:MAX_ITEMS_PER_FEED]
                for entry in entries:
                    items.append(
                        RawItem(source=self._name, payload={**entry, "feed": url}, fetched_at=now)
                    )
            except Exception as error:  # noqa: BLE001 — jeden mrtvý feed nezabije ostatní
                errors.append(f"{url}: {_describe(error)}")
        if errors and not items:
            # Všechny feedy zdroje selhaly → ať to runner započítá do degradace
            raise RuntimeError("; ".join(errors))
        if errors:
            logger.warning("Část feedů %s selhala: %s", self._name, "; ".join(errors))
        return items

    def normalize(self, item: RawItem) -> NewsEvent | None:
        title = item.payload.get("title")
        if not title:
            return None
        published = parse_feed_time(item.payload.get("published"))
        # Pozn. (#552 L2): budoucí `ts_event` se tu VĚDOMĚ nepřepisuje. Feed ho
        # sice plně ovládá, ale zahodit ho by ničilo informaci a u plánovaných
        # událostí (makro kalendář) je budoucí čas legitimní. Škodil jen tím, že
        # držel položku na vrcholu LLM fronty — to řeší řazení podle menšího
        # z (ts_event, ts_ingested) v `llm_classifier._pending`.
        summary = item.payload.get("summary")
        return NewsEvent(
            # Bez času publikace bereme čas ingestu — u RSS je rozdíl v jednotkách
            # minut a zahodit zprávu kvůli chybějícímu datu by bylo horší
            ts_event=published or item.fetched_at,
            ts_ingested=item.fetched_at,
            source=self._name,
            source_uid=item.payload.get("guid") or item.payload.get("link"),
            kind=self._kind,
            category=self._category,
            importance=self._importance,
            title=str(title)[:TITLE_LIMIT],
            summary=summary[:SUMMARY_LIMIT] if summary else None,
            symbols=list(self._symbols),
            raw=dict(item.payload),
        )
