"""Zápis pravidlové klasifikace (#280, SPEC kap. 4 + S11).

Klasifikace se **nikdy nepřepisuje** — každý průchod přidá verzi do
`news_classifications`. Pravidlový pass je verze 1; Gemini v N3 přidá verzi 2
a ruční oprava verzi 3. `news_events` drží denormalizovanou poslední verzi pro
rychlé čtení feedu, ale zdrojem pravdy je historie verzí: bez ní by zpětná
reklasifikace tiše měnila minulé predikce.
"""

import datetime as dt
import logging

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import news_classifications, news_events
from gexlens_news.classifier import classify
from gexlens_news.conventions import scheduled_direction

logger = logging.getLogger(__name__)

RULE_SOURCE = "rule"
RULE_VERSION = 1


class RuleClassificationJob:
    """Doplní pravidlovou klasifikaci eventům, které ji ještě nemají."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _pending(self, limit: int) -> list[tuple[int, str, str | None, str, float | None]]:
        already = select(news_classifications.c.event_id).where(
            news_classifications.c.source == RULE_SOURCE
        )
        stmt = (
            select(
                news_events.c.id,
                news_events.c.title,
                news_events.c.summary,
                news_events.c.kind,
                news_events.c.surprise_z,
            )
            .where(news_events.c.id.not_in(already))
            .order_by(news_events.c.ts_event.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [
            (
                int(row.id),
                row.title,
                row.summary,
                row.kind,
                float(row.surprise_z) if row.surprise_z is not None else None,
            )
            for row in rows
        ]

    def run(self, now: dt.datetime, *, limit: int = 500) -> int:
        """Zapíše verzi 1 pro nové eventy; vrací počet klasifikovaných."""
        pending = self._pending(limit)
        if not pending:
            return 0

        rows: list[dict[str, object]] = []
        updates: list[tuple[int, str, int, int, float]] = []
        for event_id, title, summary, kind, surprise_z in pending:
            result = classify(title, summary)
            direction = result.direction
            strength = result.strength
            if kind == "scheduled":
                # Plánované eventy klasifikaci směru nepotřebují — plyne
                # z překvapení a konvence řady (SPEC kap. 4)
                from_convention = scheduled_direction(title, surprise_z)
                if from_convention is not None:
                    direction = from_convention
                    strength = 0.6 if from_convention != 0 else 0.0
            rows.append(
                {
                    "event_id": event_id,
                    "version": RULE_VERSION,
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
        logger.info("Pravidlová klasifikace: %d eventů", len(rows))
        return len(rows)
