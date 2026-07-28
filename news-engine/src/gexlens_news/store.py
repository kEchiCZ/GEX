"""Zápis normalizovaných eventů do PostgreSQL (SPEC 3.1 — writer).

Duplicity se zahazují na unikátním `dedup_hash`; plnohodnotný rolling-window
dedup a cross-source merge přijde v #273. Tady jde jen o to, aby skeleton
uměl bezpečně psát a opakovaný běh nic nerozbil.
"""

import logging
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import news_events
from gexlens_news.model import NewsEvent

logger = logging.getLogger(__name__)


class NewsWriter:
    """Idempotentní zápis eventů (ON CONFLICT DO NOTHING nad `dedup_hash`)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def write(self, events: Sequence[NewsEvent]) -> int:
        if not events:
            return 0
        rows = [
            {
                "ts_event": event.ts_event,
                "ts_ingested": event.ts_ingested,
                "source": event.source,
                "source_uid": event.source_uid,
                "kind": event.kind,
                "category": event.category,
                "importance": event.importance,
                "title": event.title,
                "summary": event.summary,
                "symbols": event.symbols,
                "forecast": event.forecast,
                "previous": event.previous,
                "actual": event.actual,
                "surprise_z": event.surprise_z,
                "market_closed": event.market_closed,
                "dedup_hash": event.dedup_hash,
                "raw": event.raw,
            }
            for event in events
        ]
        dialect = self._engine.dialect.name
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        written = 0
        with self._engine.begin() as conn:
            for row in rows:
                stmt = (
                    insert(news_events)
                    .values(**row)
                    .on_conflict_do_nothing(index_elements=[news_events.c.dedup_hash])
                )
                written += conn.execute(stmt).rowcount or 0
        skipped = len(rows) - written
        if skipped:
            logger.debug("Zahozeno %d duplicit dle dedup_hash", skipped)
        return written

    def count(self) -> int:
        with self._engine.connect() as conn:
            total = conn.execute(select(func.count()).select_from(news_events)).scalar()
        return int(total or 0)
