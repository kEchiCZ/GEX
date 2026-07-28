"""Broker headlines z generic ticku 292 (#291, SPEC kap. 1 Tier D).

Zachytává je **datový engine**, ne news-engine: připojení k IBKR má engine
a market data lines jsou limit na účet, ne na spojení (ADR-0001), takže druhý
clientId by kapacitu nepřidal — jen rozdělil tutéž mezi dva procesy, které
o sobě nevědí. Zápis jde do sdílené `news_events`, odkud si je news-engine
přečte stejně jako zprávy z vlastních collectorů.

Motivace je měřená: RSS zdroje doručují headline s mediánem 667 s po jejich
vlastním `pubDate` (změřeno 28. 7. na 371 zprávách), takže požadavek
„headline → DB < 60 s" plnilo 11 z 371. Broker tick chodí živě.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from gexlens_engine.compute.newstext import dedup_hash
from gexlens_engine.storage.sentiment import news_events

logger = logging.getLogger(__name__)

# Generic tick pro živé headlines (IBKR `mdoff,292`)
NEWS_TICK = "292"
# Kolik článků se drží v paměti jako „už zapsané"; IBKR seznam roste přes den
SEEN_LIMIT = 5000
# Nad touhle hranicí je epoch v milisekundách, ne sekundách (rok 2001 v ms)
EPOCH_MS_THRESHOLD = 1e12


def tick_time(raw: object, *, now: dt.datetime) -> dt.datetime:
    """Čas z NewsTicku; IBKR ho posílá jako **epoch int**, ne datetime.

    Zvládá sekundy i milisekundy — providery se v jednotce liší a špatná
    jednotka by zprávu posunula o desítky let. Nečitelná hodnota → čas přijetí,
    protože zahodit headline kvůli timestampu by bylo horší než pár sekund
    nepřesnosti.
    """
    if isinstance(raw, dt.datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=dt.UTC)
    if isinstance(raw, (int, float)) and raw > 0:
        seconds = float(raw) / 1000.0 if float(raw) >= EPOCH_MS_THRESHOLD else float(raw)
        try:
            return dt.datetime.fromtimestamp(seconds, tz=dt.UTC)
        except (OverflowError, OSError, ValueError):
            logger.debug("Nečitelný timestamp headline: %r", raw)
    return now


class NewsTickLike(Protocol):
    """Tvar `ib_async.NewsTick`.

    Atributy jsou jen ke čtení — `NewsTick` je NamedTuple, takže zapisovatelné
    atributy v protokolu by se s ním typově nesešly.
    """

    @property
    def timeStamp(self) -> int: ...

    @property
    def providerCode(self) -> str: ...

    @property
    def articleId(self) -> str: ...

    @property
    def headline(self) -> str: ...


@dataclass(frozen=True)
class BrokerHeadline:
    """Normalizovaná broker zpráva připravená k zápisu."""

    ts_event: dt.datetime
    provider: str
    article_id: str
    title: str

    @property
    def source(self) -> str:
        return f"ibkr_{self.provider.lower()}"


def clean_headline(raw: str) -> str:
    """Titulek bez prefixu providera.

    IBKR posílá `!DJ-RTG headline…` nebo `{A:1}headline`; do DB patří text,
    ne interní značky, jinak by se dedup nesešel s toutéž story z RSS.
    """
    text = raw.strip()
    if text.startswith("!"):
        # `!PROVIDER text` — odřízne token do první mezery
        parts = text.split(" ", 1)
        text = parts[1] if len(parts) > 1 else ""
    while text.startswith("{"):
        end = text.find("}")
        if end == -1:
            break
        text = text[end + 1 :]
    return text.strip()


def normalize_tick(tick: NewsTickLike, *, now: dt.datetime) -> BrokerHeadline | None:
    """NewsTick → `BrokerHeadline`; None = nepoužitelný záznam.

    Bez času bereme čas přijetí — u živého ticku je rozdíl v sekundách
    a zahodit zprávu kvůli chybějícímu timestampu by bylo horší.
    """
    title = clean_headline(getattr(tick, "headline", "") or "")
    if not title:
        return None
    return BrokerHeadline(
        ts_event=tick_time(getattr(tick, "timeStamp", None), now=now),
        provider=(getattr(tick, "providerCode", "") or "unknown").strip(),
        article_id=(getattr(tick, "articleId", "") or "").strip(),
        title=title,
    )


class NewsTickCollector:
    """Čte `ib.newsTicks()` v minutovém cyklu a zapisuje nové do `news_events`.

    Polling místo event handleru záměrně: engine má minutový cyklus a IBKR
    seznam je kumulativní, takže stačí sledovat, co už jsme viděli.
    """

    def __init__(self, db: Engine) -> None:
        self._db = db
        self._seen: set[str] = set()

    def _key(self, headline: BrokerHeadline) -> str:
        # articleId je stabilní; bez něj (starší providery) padáme na hash
        return headline.article_id or dedup_hash(headline.title, headline.ts_event)

    def write(self, ticks: Sequence[NewsTickLike], *, now: dt.datetime) -> int:
        """Zapíše nové headlines; vrací počet skutečně uložených."""
        rows: list[dict[str, Any]] = []
        for tick in ticks:
            headline = normalize_tick(tick, now=now)
            if headline is None:
                continue
            key = self._key(headline)
            if key in self._seen:
                continue
            self._seen.add(key)
            rows.append(
                {
                    "ts_event": headline.ts_event,
                    "ts_ingested": now,
                    "source": headline.source,
                    "source_uid": headline.article_id or None,
                    "kind": "broker",
                    "category": None,  # doplní klasifikátor news-engine
                    "importance": None,
                    "title": headline.title,
                    "summary": None,
                    "symbols": [],
                    "market_closed": False,
                    "dedup_hash": dedup_hash(headline.title, headline.ts_event),
                    "raw": {
                        "provider": headline.provider,
                        "article_id": headline.article_id,
                    },
                }
            )
        if len(self._seen) > SEEN_LIMIT:
            self._seen.clear()
        if not rows:
            return 0

        insert = pg_insert if self._db.dialect.name == "postgresql" else sqlite_insert
        written = 0
        with self._db.begin() as conn:
            for row in rows:
                stmt = (
                    insert(news_events)
                    .values(**row)
                    .on_conflict_do_nothing(index_elements=[news_events.c.dedup_hash])
                )
                written += conn.execute(stmt).rowcount or 0
        if written:
            logger.info("IBKR headlines: %d nových (z %d ticků)", written, len(rows))
        return written

    def count(self) -> int:
        with self._db.connect() as conn:
            total = conn.execute(
                select(func.count()).select_from(news_events).where(news_events.c.kind == "broker")
            ).scalar()
        return int(total or 0)
