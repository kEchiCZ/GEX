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
    """Doplní pravidlovou klasifikaci eventům, které ji ještě nemají."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        # Poslední dávka pro push do WS (#335). Držet ji tady je jednodušší než
        # měnit návratový typ `run` — ten čte i retro pass, kterému stačí počet.
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
            select(
                news_events.c.id,
                news_events.c.title,
                news_events.c.summary,
                news_events.c.kind,
                news_events.c.surprise_z,
                # Pro push do WS (#335) — UI potřebuje celý řádek, ne jen kategorii
                news_events.c.ts_event,
                news_events.c.source,
                *RAW_COLUMNS,
            )
            .where(~already)
            .order_by(news_events.c.ts_event.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            return list(conn.execute(stmt).fetchall())

    def run(self, now: dt.datetime, *, limit: int = 500) -> int:
        """Zapíše verzi 1 pro nové eventy; vrací počet klasifikovaných."""
        pending = self._pending(limit)
        self.last_batch = []
        if not pending:
            return 0

        # Verze = max+1 per event (S11): LLM pass mohl event klasifikovat dřív
        # (po FF backfillu #277 pravidlový pass nestíhal) a natvrdo zapsaná
        # verze 1 pak shazovala celou dávku na UniqueViolation (#373)
        with self._engine.connect() as conn:
            versions = {
                int(event_id): int(version)
                for event_id, version in conn.execute(
                    select(
                        news_classifications.c.event_id,
                        func.max(news_classifications.c.version),
                    )
                    .where(news_classifications.c.event_id.in_([int(row.id) for row in pending]))
                    .group_by(news_classifications.c.event_id)
                )
            }

        rows: list[dict[str, object]] = []
        updates: list[tuple[int, str, int, int, float]] = []
        batch: list[dict[str, object]] = []
        for row in pending:
            event_id = int(row.id)
            result = classify_row(row)
            direction = result.direction
            strength = result.strength
            batch.append(
                {
                    "id": event_id,
                    "ts_event": row.ts_event.isoformat(),
                    "ts_ingested": row.ts_event.isoformat(),
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
                    "version": versions.get(event_id, 0) + 1,
                    "source": RULE_SOURCE,
                    "category": result.category,
                    "importance": result.importance,
                    "direction": direction,
                    "strength": strength,
                    "created_at": now,
                }
            )
            updates.append((event_id, result.category, result.importance, direction, strength))

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
        self.last_batch = batch
        logger.info("Pravidlová klasifikace: %d eventů", len(rows))
        return len(rows)
