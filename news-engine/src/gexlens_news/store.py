"""Zápis normalizovaných eventů do PostgreSQL (SPEC 3.1 — writer).

Duplicity se zahazují na unikátním `dedup_hash` (opakovaný běh nic nerozbije);
rolling-window dedup před zápisem drží `pipeline.DedupingWriter`. Kopie téže
zprávy z jiného zdroje se zapíše do `news_event_sources` (ADR-0059 bod 3), ať
ji zahodil rolling dedup (`write_copies`), nebo unikátní `dedup_hash` (`write`).
"""

import datetime as dt
import logging
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from gexlens_engine.compute.news_tier import content_tier
from gexlens_engine.compute.newstext import normalize_source_uid
from gexlens_engine.storage.sentiment import CopyOutcome, news_events, record_news_copy
from gexlens_news.dedup import DedupCopy
from gexlens_news.http import sanitize_raw
from gexlens_news.model import NewsEvent

logger = logging.getLogger(__name__)


def _as_utc(value: dt.datetime) -> dt.datetime:
    """Čas z DB vždy jako tz-aware UTC.

    PostgreSQL vrací offset-aware, sqlite (testy) naivní — bez sjednocení by
    porovnání časů v dedup okně padalo na TypeError.
    """
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _record_copy(conn: Connection, event: NewsEvent, *, first_hash: str) -> CopyOutcome:
    """Kopie `event` k prvnímu doručení s `first_hash` (ADR-0059 bod 3).

    Tier a čas publikace jsou kopie samotné: `published_at` = její `ts_event`
    (pravidla jejího zdroje), viditelná je od `ts_ingested`.
    """
    return record_news_copy(
        conn,
        dedup_hash=first_hash,
        source=event.source,
        source_uid=normalize_source_uid(event.source_uid),
        content_tier=content_tier(event.source, event.raw),
        published_at=event.ts_event,
        fetched_at=event.ts_ingested,
    )


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
                "source_uid": normalize_source_uid(event.source_uid),
                "kind": event.kind,
                "category": event.category,
                "importance": event.importance,
                "title": event.title,
                "summary": event.summary,
                "body": event.body,
                "symbols": event.symbols,
                "forecast": event.forecast,
                "previous": event.previous,
                "actual": event.actual,
                "surprise_z": event.surprise_z,
                "market_closed": event.market_closed,
                "content_tier": content_tier(event.source, event.raw),
                "dedup_hash": event.dedup_hash,
                # S10 (#553): raw payload nesmí do DB s tokenem v URL — čistí
                # se až tady na zápisu, jediné hrdlo pro všechny collectory
                "raw": sanitize_raw(event.raw),
            }
            for event in events
        ]
        dialect = self._engine.dialect.name
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        written = 0
        copies = 0
        with self._engine.begin() as conn:
            for event, row in zip(events, rows, strict=True):
                # RETURNING, ne rowcount: PostgreSQL u ON CONFLICT DO NOTHING
                # vrací -1 (= „nevím") a počítadlo by lhalo (#367)
                stmt = (
                    insert(news_events)
                    .values(**row)
                    .on_conflict_do_nothing(index_elements=[news_events.c.dedup_hash])
                    .returning(news_events.c.id)
                )
                if conn.execute(stmt).first() is not None:
                    written += 1
                elif _record_copy(conn, event, first_hash=event.dedup_hash) == "recorded":
                    # Týž titulek téhož dne už v DB je (mimo okno rolling dedupu,
                    # IBKR páska z enginu, zápis mimo DedupingWriter)
                    copies += 1
        skipped = len(rows) - written
        if skipped:
            logger.debug(
                "Zahozeno %d duplicit dle dedup_hash, z toho %d kopií z jiného zdroje",
                skipped,
                copies,
            )
        return written

    def write_copies(self, copies: Sequence[DedupCopy]) -> int:
        """Kopie zahozené rolling dedupem do `news_event_sources`; vrací počet nových.

        Kopie bez prvního doručení v DB (jeho zápis selhal) se nezapíše a jde
        do logu jako WARNING — tichá ztráta by zkreslila efektivní tier.
        """
        recorded = 0
        missing: list[str] = []
        with self._engine.begin() as conn:
            for copy in copies:
                outcome = _record_copy(conn, copy.event, first_hash=copy.first_hash)
                if outcome == "recorded":
                    recorded += 1
                elif outcome == "missing":
                    missing.append(copy.event.source)
        if missing:
            logger.warning(
                "Kopie zprávy bez prvního doručení v DB — nezapsáno %d (zdroje %s)",
                len(missing),
                ", ".join(sorted(set(missing))),
            )
        return recorded

    def recent(self, since: dt.datetime) -> list[NewsEvent]:
        """Eventy od `since` — naplní dedup okno po startu (#273).

        Bez toho by restart procesu zapsal duplicity ke všemu, co dorazilo
        v posledních minutách.
        """
        stmt = select(
            news_events.c.ts_event,
            news_events.c.ts_ingested,
            news_events.c.source,
            news_events.c.source_uid,
            news_events.c.kind,
            news_events.c.title,
        ).where(news_events.c.ts_event >= since)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [
            NewsEvent(
                ts_event=_as_utc(row.ts_event),
                ts_ingested=_as_utc(row.ts_ingested),
                source=row.source,
                source_uid=row.source_uid,
                kind=row.kind,
                title=row.title,
            )
            for row in rows
        ]

    def count(self) -> int:
        with self._engine.connect() as conn:
            total = conn.execute(select(func.count()).select_from(news_events)).scalar()
        return int(total or 0)
