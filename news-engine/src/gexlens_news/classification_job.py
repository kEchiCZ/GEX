"""Zápis pravidlové klasifikace (#280, SPEC kap. 4 + S11).

Klasifikace se **nikdy nepřepisuje** — každý průchod přidá verzi do
`news_classifications`. Pravidlový pass je verze 1; Gemini v N3 přidá verzi 2
a ruční oprava verzi 3. `news_events` drží denormalizovanou poslední verzi pro
rychlé čtení feedu, ale zdrojem pravdy je historie verzí: bez ní by zpětná
reklasifikace tiše měnila minulé predikce.

Klasifikátor v2 (#1293, ADR-0045) potřebuje kromě titulku **feed** (strop
importance: sociální sítě bez kurátora, agregátory) a u kalendáře **FF impact**
— obojí je v surovém payloadu (`RAW_COLUMNS`). Kalendář ani `fed_rss` se už
regexem nad titulkem nepřepisují. Reklasifikace historie
(`scripts/reclassify_news_rules.py`) volá tutéž `classify_row`.
"""

import datetime as dt
import logging
import statistics
import threading
from typing import Any

from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.engine import Engine

from gexlens_engine.compute.news_significance import significance_tier
from gexlens_engine.storage.sentiment import news_classifications, news_events
from gexlens_news.classifier import RuleClassification, classify, feed_of
from gexlens_news.conventions import scheduled_direction

logger = logging.getLogger(__name__)

RULE_SOURCE = "rule"
SCHEDULED_KIND = "scheduled"
#: Síla směru z konvence řady u kalendáře (překvapení × polarita, SPEC kap. 4)
SCHEDULED_STRENGTH = 0.6

#: Pole surového payloadu, která klasifikace čte: feed (`raw.feed` URL RSS feedu,
#: záložně `raw.link`), kurátor Bluesky (`raw.curated`, autor `raw.did`) a FF
#: impact (živý feed `raw.impact`, backfill `raw.impactName`)
RAW_COLUMNS = (
    news_events.c.raw["feed"].as_string().label("raw_feed"),
    news_events.c.raw["link"].as_string().label("raw_link"),
    news_events.c.raw["curated"].as_boolean().label("raw_curated"),
    news_events.c.raw["did"].as_string().label("raw_did"),
    news_events.c.raw["impact"].as_string().label("raw_impact"),
    news_events.c.raw["impactName"].as_string().label("raw_impact_name"),
)
#: Sloupce pro klasifikaci i push do WS (#335) — UI potřebuje celý řádek, ne jen kategorii
ROW_COLUMNS = (
    news_events.c.id,
    news_events.c.title,
    news_events.c.summary,
    news_events.c.kind,
    news_events.c.surprise_z,
    news_events.c.ts_event,
    news_events.c.ts_ingested,
    news_events.c.source,
    *RAW_COLUMNS,
)
#: Jak často rychlá cesta loguje latenci od příjmu (#1496)
LATENCY_LOG_EVERY = dt.timedelta(minutes=10)


def row_feed(row: Any, curated_dids: frozenset[str] = frozenset()) -> str:
    """Feed řádku (`feed_of`) — klíč stropu importance.

    Kurátora Bluesky pozná podle příznaku `raw.curated`, který collector
    zapisuje až od #1291 (25. 9. 2026). Reklasifikace historie předává
    `curated_dids` (aktuální seznam kurátorů), aby starší posty týchž autorů
    nedostaly strop nekurátorované sociální sítě.
    """
    curated = row.raw_curated is True or (row.raw_did is not None and row.raw_did in curated_dids)
    raw = {"feed": row.raw_feed, "link": row.raw_link, "curated": curated}
    return feed_of(str(row.source), raw)


def classify_row(row: Any, curated_dids: frozenset[str] = frozenset()) -> RuleClassification:
    """Pravidlová klasifikace řádku se sloupci `title`, `source`, `kind`,
    `surprise_z` a `RAW_COLUMNS` — sdílí ji job i reklasifikace historie.

    U kalendáře směr plyne z překvapení a konvence řady, ne ze slovesa.
    `curated_dids` viz `row_feed` (jen reklasifikace historie).
    """
    result = classify(
        str(row.title or ""),
        feed=row_feed(row, curated_dids),
        kind=str(row.kind),
        ff_impact=row.raw_impact_name or row.raw_impact,
    )
    if row.kind != SCHEDULED_KIND:
        return result
    surprise_z = float(row.surprise_z) if row.surprise_z is not None else None
    from_convention = scheduled_direction(str(row.title or ""), surprise_z)
    if from_convention is None:
        return result
    strength = SCHEDULED_STRENGTH if from_convention != 0 else 0.0
    return RuleClassification(
        result.category, result.importance, from_convention, strength, result.reason
    )


class RuleClassificationJob:
    """Doplní pravidlovou klasifikaci eventům, které ji ještě nemají.

    Dvě cesty a jeden zápis (`_classify`):

    * **rychlá** (`run_new`, smyčka à 1 s, #1496): jen řádky s `id` nad
      watermarkem — dotaz přes PK bez anti-joinu, takže nová zpráva má
      kategorii a importance do ~1 s od zápisu (karta Breaking news, E-6.28);
    * **pojistka** (`run`, `reaction_loop` à 300 s a retro pass): anti-join přes
      celou tabulku. Chytí řádek commitnutý s nižším `id` až po posunu
      watermarku (dlouhá transakce, backfill) i výpadek rychlé smyčky.

    Cesty běží v různých vláknech (`asyncio.to_thread`), proto zápis drží zámek
    a pod ním znovu ověří, že event klasifikaci nemá — jinak by souběh zapsal
    pravidlovou verzi dvakrát.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._lock = threading.Lock()
        # Watermark rychlé cesty; None = ještě nenastavený (první `run_new`)
        self._watermark: int | None = None
        # Latence `now − ts_ingested` rychlé cesty pro log à 10 min (#1496)
        self._latencies: list[float] = []
        self._latency_logged_at: dt.datetime | None = None
        # Poslední dávka pojistky pro push do WS (#335). Držet ji tady je
        # jednodušší než měnit návratový typ `run` — ten čte i retro pass, kterému
        # stačí počet. Rychlá cesta dávku vrací: sdílené pole by si souběžné
        # smyčky přepisovaly.
        self.last_batch: list[dict[str, object]] = []

    def _pending(self, limit: int) -> list[Any]:
        # Jakákoli existující klasifikace event vyřazuje (#373): pravidlový
        # pass je PRVNÍ průchod/fallback — event, který už má LLM verzi,
        # nepotřebuje hrubší pravidlovou navrch (a denormalizace by regresí
        # z llm na rule lhala). Scheduled eventy LLM nikdy nebere, takže směr
        # ze surprise_z dostávají vždy tady.
        # NOT EXISTS místo NOT IN (#1257): `NOT IN (subquery)` PostgreSQL hashuje
        # jen když se hash vejde do work_mem; nad ~260 k klasifikací (23. 9. 2026)
        # spadl na Materialize + Filter per řádek a jeden běh jobu trval 23+ min
        # na ~190 % CPU. Anti-join přes index (event_id, version) je na velikosti
        # nezávislý.
        already = exists().where(news_classifications.c.event_id == news_events.c.id)
        stmt = (
            select(*ROW_COLUMNS)
            .where(~already)
            .order_by(news_events.c.ts_event.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            return list(conn.execute(stmt).fetchall())

    def run(self, now: dt.datetime, *, limit: int = 500) -> int:
        """Pojistka: zapíše verzi 1 eventům bez klasifikace; vrací počet."""
        # Drahý anti-join (~0,5 s na produkci) běží mimo zámek, aby rychlou
        # cestu neblokoval; souběh ošetří kontrola v `_classify`
        pending = self._pending(limit)
        self.last_batch = []
        if not pending:
            return 0
        self.last_batch = self._classify(pending, now)
        if self.last_batch:
            logger.info("Pravidlová klasifikace: %d eventů", len(self.last_batch))
        return len(self.last_batch)

    def run_new(self, now: dt.datetime, *, limit: int = 500) -> list[dict[str, object]]:
        """Rychlá cesta (#1496): klasifikuje řádky s `id` nad watermarkem.

        Vrací dávku pro push do WS. První volání jen nastaví watermark na
        `max(id)` — co bylo v DB před startem, dojede pojistka (`run`).
        """
        if self._watermark is None:
            with self._engine.connect() as conn:
                top = conn.execute(select(func.max(news_events.c.id))).scalar()
            self._watermark = int(top or 0)
            self._latency_logged_at = now
            return []
        stmt = (
            select(*ROW_COLUMNS)
            .where(news_events.c.id > self._watermark)
            .order_by(news_events.c.id)
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = list(conn.execute(stmt).fetchall())
        batch: list[dict[str, object]] = []
        if rows:
            self._watermark = max(int(row.id) for row in rows)
            batch = self._classify(rows, now)
            classified = {item["id"] for item in batch}
            self._latencies.extend(
                (now - _as_utc(row.ts_ingested)).total_seconds()
                for row in rows
                if int(row.id) in classified
            )
        self._log_latency(now)
        return batch

    def _log_latency(self, now: dt.datetime) -> None:
        """Medián, p90 a max latence rychlé cesty do logu à 10 min (#1496)."""
        if self._latency_logged_at is None:
            self._latency_logged_at = now
        if now - self._latency_logged_at < LATENCY_LOG_EVERY or not self._latencies:
            return
        values = sorted(self._latencies)
        logger.info(
            "Rychlá klasifikace: %d eventů, od příjmu medián %.1f s, p90 %.1f s, max %.1f s",
            len(values),
            statistics.median(values),
            values[min(len(values) - 1, int(0.9 * len(values)))],
            values[-1],
        )
        self._latencies = []
        self._latency_logged_at = now

    def _classify(self, pending: list[Any], now: dt.datetime) -> list[dict[str, object]]:
        """Zapíše pravidlovou verzi eventům, které klasifikaci stále nemají.

        Vrací dávku pro push do WS (#335), jen skutečně zapsané eventy.
        """
        with self._lock:
            return self._classify_locked(pending, now)

    def _classify_locked(self, pending: list[Any], now: dt.datetime) -> list[dict[str, object]]:
        # Jakákoli klasifikace event vyřazuje stejně jako `_pending` (#373):
        # mezi výběrem a zápisem ho mohla klasifikovat druhá cesta (#1496) nebo
        # LLM pass. Natvrdo zapsaná verze 1 nad existující by shodila celou
        # dávku na UniqueViolation (#373), proto se kontroluje až tady
        with self._engine.connect() as conn:
            classified = {
                int(event_id)
                for (event_id,) in conn.execute(
                    select(news_classifications.c.event_id)
                    .where(news_classifications.c.event_id.in_([int(row.id) for row in pending]))
                    .distinct()
                )
            }

        rows: list[dict[str, object]] = []
        updates: list[tuple[int, str, int, int, float]] = []
        batch: list[dict[str, object]] = []
        for row in pending:
            if int(row.id) in classified:
                continue
            event_id = int(row.id)
            result = classify_row(row)
            direction = result.direction
            strength = result.strength
            batch.append(
                {
                    "id": event_id,
                    "ts_event": row.ts_event.isoformat(),
                    # Skutečný čas příjmu — karta z něj počítá zpoždění zdroje (#1496)
                    "ts_ingested": _as_utc(row.ts_ingested).isoformat(),
                    "source": row.source,
                    "kind": row.kind,
                    "category": result.category,
                    "importance": result.importance,
                    # Stupeň významnosti (#1305) — graf filtruje podle něj, ne podle
                    # vlastní kopie pravidel
                    "significance": significance_tier(row.kind, result.importance, result.category),
                    "title": row.title,
                    "summary": row.summary,
                    "sentiment_dir": direction,
                    "sentiment_score": direction * strength,
                    "sentiment_source": RULE_SOURCE,
                    "forecast": None,
                    "previous": None,
                    "actual": None,
                }
            )
            rows.append(
                {
                    "event_id": event_id,
                    "version": 1,
                    "source": RULE_SOURCE,
                    "category": result.category,
                    "importance": result.importance,
                    "direction": direction,
                    "strength": strength,
                    "created_at": now,
                }
            )
            updates.append((event_id, result.category, result.importance, direction, strength))

        if not rows:
            return []
        with self._engine.begin() as conn:
            conn.execute(insert(news_classifications), rows)
            for event_id, category, importance, direction, strength in updates:
                conn.execute(
                    update(news_events)
                    .where(news_events.c.id == event_id)
                    .values(
                        category=category,
                        importance=importance,
                        sentiment_dir=direction,
                        # Skóre = směr × síla (SPEC 5.3 bez vah, ty přijdou s
                        # kalibrací); denormalizace pro rychlé čtení feedu
                        sentiment_score=direction * strength,
                        sentiment_source=RULE_SOURCE,
                    )
                )
        return batch


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
