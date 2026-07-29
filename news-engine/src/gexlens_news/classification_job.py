"""Zápis pravidlové klasifikace (#280, SPEC kap. 4 + S11).

Klasifikace se **nikdy nepřepisuje** — každý průchod přidá verzi do
`news_classifications`. Pravidlový pass je verze 1; Gemini v N3 přidá verzi 2
a ruční oprava verzi 3. `news_events` drží denormalizovanou poslední verzi pro
rychlé čtení feedu, ale zdrojem pravdy je historie verzí: bez ní by zpětná
reklasifikace tiše měnila minulé predikce.
"""

import datetime as dt
import logging
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import news_classifications, news_events
from gexlens_news.classifier import classify
from gexlens_news.conventions import scheduled_direction

logger = logging.getLogger(__name__)

RULE_SOURCE = "rule"


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
        already = select(news_classifications.c.event_id)
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
            )
            .where(news_events.c.id.not_in(already))
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
            title = row.title
            surprise_z = float(row.surprise_z) if row.surprise_z is not None else None
            result = classify(title, row.summary)
            direction = result.direction
            strength = result.strength
            if row.kind == "scheduled":
                # Plánované eventy klasifikaci směru nepotřebují — plyne
                # z překvapení a konvence řady (SPEC kap. 4)
                from_convention = scheduled_direction(title, surprise_z)
                if from_convention is not None:
                    direction = from_convention
                    strength = 0.6 if from_convention != 0 else 0.0
            batch.append(
                {
                    "id": event_id,
                    "ts_event": row.ts_event.isoformat(),
                    "ts_ingested": row.ts_event.isoformat(),
                    "source": row.source,
                    "kind": row.kind,
                    "category": result.category,
                    "importance": result.importance,
                    "title": title,
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
