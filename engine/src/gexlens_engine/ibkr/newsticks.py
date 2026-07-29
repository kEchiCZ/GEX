"""Broker headlines z generic ticku 292 (#291, SPEC kap. 1 Tier D).

Zachytává je **datový engine**, ne news-engine: připojení k IBKR má engine
a market data lines jsou limit na účet, ne na spojení (ADR-0001), takže druhý
clientId by kapacitu nepřidal — jen rozdělil tutéž mezi dva procesy, které
o sobě nevědí. Zápis jde do sdílené `news_events`, odkud si je news-engine
přečte stejně jako zprávy z vlastních collectorů.

Motivace je měřená: RSS zdroje doručují headline s mediánem 667 s po jejich
vlastním `pubDate` (změřeno 28. 7. na 371 zprávách), takže požadavek
„headline → DB < 60 s" plnilo 11 z 371. Broker tick chodí živě.

**Odebírá se broad tape providera, ne podklad** (#334). Na futures IBKR news
subskripci odmítá (`Error 10094: Derivative contracts cannot be used to
subscribe to news`) a akciová proxy (SPY) padá na chybějícím předplatném US
equity dat (`Error 10089`). Zbývá `secType='NEWS'` — celá páska providera,
nezávislá na podkladu. To je pro makro sentiment stejně to, co chceme: zprávy
hýbající ES/NQ nejsou vázané na jeden kontrakt.
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

from gexlens_engine.compute.marketclock import is_market_closed
from gexlens_engine.compute.newstext import dedup_hash, normalize_source_uid
from gexlens_engine.storage.sentiment import news_events

logger = logging.getLogger(__name__)

# Generic ticks broad tape. `mdoff` vypíná kotace — NEWS kontrakt žádné nemá
# a bez něj IBKR subskripci odmítne.
BROAD_TAPE_TICKS = "mdoff,292"
# Přípona „celá páska providera"; `BRFG` → `BRFG:BRFG_ALL`
BROAD_TAPE_SUFFIX = "_ALL"
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


def tape_symbol(provider: str) -> str:
    """`BRFG` → `BRFG:BRFG_ALL` — symbol celé pásky providera."""
    return f"{provider}:{provider}{BROAD_TAPE_SUFFIX}"


class MarketDataClient(Protocol):
    """Nízkoúrovňový klient ib_async (`ib.client`)."""

    def getReqId(self) -> int: ...

    def reqMktData(
        self,
        reqId: int,
        contract: Any,
        genericTickList: str,
        snapshot: bool,
        regulatorySnapshot: bool,
        mktDataOptions: list[Any],
    ) -> None: ...


def subscribe_broad_tape(
    client: MarketDataClient, providers: Sequence[str], *, make_contract: Any
) -> list[str]:
    """Odebere pásku každého providera; vrací ty, u kterých request odešel.

    Jde se přes `ib.client`, ne přes `IB.reqMktData`: ten si kontrakt ukládá do
    registru tickerů podle `conId`, který NEWS kontrakt nemá a `reqContractDetails`
    ho nedoplní (na NEWS kontrakt vůbec neodpoví). `Wrapper.tickNews` ale reqId
    ignoruje a do `newsTicks` zapisuje bezpodmínečně, takže registr není potřeba.

    Provider bez `_ALL` pásky (`BRFUPDN` — upgrady/downgrady) odpoví **asynchronně**
    `Error 200: No security definition`. Tady se to nepozná a poznat nemá:
    zahodit kvůli jednomu providerovi ostatní by bylo horší než chyba v logu.
    """
    subscribed: list[str] = []
    for provider in providers:
        req_id = client.getReqId()
        try:
            client.reqMktData(req_id, make_contract(provider), BROAD_TAPE_TICKS, False, False, [])
        except Exception:
            logger.exception("Subskripce pásky %s selhala — pokračuji dalším", provider)
            continue
        subscribed.append(provider)
    logger.info("Broker news: odebráno %d pásek (%s)", len(subscribed), ", ".join(subscribed))
    return subscribed


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


@dataclass(frozen=True)
class StoredHeadline:
    """Zapsaná zpráva i s `id` z DB — podklad pro okamžitý push do UI."""

    id: int
    headline: BrokerHeadline

    def as_news_row(self) -> dict[str, Any]:
        """Payload kanálu `news` ve tvaru `NewsRow` frontendu.

        Kategorie je `None` schválně: klasifikátor běží až v news-engine
        a čekat na něj by zprávu zdrželo o minuty. UI ji zobrazí jako
        „Nezařazeno" a doplní se, až dorazí klasifikovaná verze téhož `id`.
        """
        return {
            "id": self.id,
            "ts_event": self.headline.ts_event.isoformat(),
            "ts_ingested": self.headline.ts_event.isoformat(),
            "source": self.headline.source,
            "kind": "broker",
            "category": None,
            "importance": None,
            "title": self.headline.title,
            "summary": None,
            "sentiment_dir": None,
            "sentiment_score": None,
            "sentiment_source": None,
            "forecast": None,
            "previous": None,
            "actual": None,
        }


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
    """Zapisuje broker headlines do `news_events`.

    Volá se ze dvou míst a to je záměr (#335):

    * z handleru `ib.tickNewsEvent` **hned**, jak tick dorazí — minuta zdržení
      je na minutovém timeframu pozdě,
    * z minutového cyklu jako pojistka proti ztracenému eventu (výpadek loopu,
      výjimka v handleru).

    Dvojí zápis nevadí: `_seen` a UNIQUE na `dedup_hash` ho zahodí.
    """

    def __init__(self, db: Engine) -> None:
        self._db = db
        self._seen: set[str] = set()

    def _key(self, headline: BrokerHeadline) -> str:
        # articleId je stabilní; bez něj (starší providery) padáme na hash
        return headline.article_id or dedup_hash(headline.title, headline.ts_event)

    def write(self, ticks: Sequence[NewsTickLike], *, now: dt.datetime) -> list[StoredHeadline]:
        """Zapíše nové headlines; vrací **skutečně uložené** i s jejich `id`.

        Vrací se záznamy, ne počet, protože volající je rovnou pushuje do WS
        kanálu (#335) — a pushnout se smí jen to, co v DB opravdu přibylo.
        """
        pending: list[tuple[BrokerHeadline, dict[str, Any]]] = []
        for tick in ticks:
            headline = normalize_tick(tick, now=now)
            if headline is None:
                continue
            key = self._key(headline)
            if key in self._seen:
                continue
            self._seen.add(key)
            pending.append(
                (
                    headline,
                    {
                        "ts_event": headline.ts_event,
                        "ts_ingested": now,
                        "source": headline.source,
                        "source_uid": normalize_source_uid(headline.article_id or None),
                        "kind": "broker",
                        "category": None,  # doplní klasifikátor news-engine
                        "importance": None,
                        "title": headline.title,
                        "summary": None,
                        "symbols": [],
                        "market_closed": is_market_closed(headline.ts_event),
                        "dedup_hash": dedup_hash(headline.title, headline.ts_event),
                        "raw": {
                            "provider": headline.provider,
                            "article_id": headline.article_id,
                        },
                    },
                )
            )
        if len(self._seen) > SEEN_LIMIT:
            self._seen.clear()
        if not pending:
            return []

        insert = pg_insert if self._db.dialect.name == "postgresql" else sqlite_insert
        written: list[StoredHeadline] = []
        with self._db.begin() as conn:
            for headline, row in pending:
                # RETURNING, ne rowcount: PostgreSQL u ON CONFLICT DO NOTHING
                # vrací -1 (= „nevím"), takže počítadlo by lhalo
                stmt = (
                    insert(news_events)
                    .values(**row)
                    .on_conflict_do_nothing(index_elements=[news_events.c.dedup_hash])
                    .returning(news_events.c.id)
                )
                inserted = conn.execute(stmt).first()
                if inserted is not None:
                    written.append(StoredHeadline(id=int(inserted.id), headline=headline))
        if written:
            logger.info("IBKR headlines: %d nových (z %d ticků)", len(written), len(pending))
        return written

    def count(self) -> int:
        with self._db.connect() as conn:
            total = conn.execute(
                select(func.count()).select_from(news_events).where(news_events.c.kind == "broker")
            ).scalar()
        return int(total or 0)
