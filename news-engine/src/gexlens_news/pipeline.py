"""Zápisová cesta: dedup → writer (#273).

Runner volá jediný `Writer` callable; tady se mezi normalizaci a databázi
vkládá rolling-window deduplikace, aby se tatáž story z více zdrojů zapsala
jednou a ostatní zdroje se k ní jen přilepily (SPEC 3.1 pořadí:
normalizer → dedup → writer).
"""

import datetime as dt
import logging
from collections.abc import Sequence

from gexlens_news.dedup import DEFAULT_WINDOW_MINUTES, RollingDeduplicator
from gexlens_news.model import NewsEvent
from gexlens_news.store import NewsWriter

logger = logging.getLogger(__name__)


class DedupingWriter:
    """Writer s pamětí nedávných stories; podpis odpovídá `runner.Writer`."""

    def __init__(
        self,
        writer: NewsWriter,
        *,
        window_minutes: int = DEFAULT_WINDOW_MINUTES,
    ) -> None:
        self._writer = writer
        self._dedup = RollingDeduplicator(window_minutes=window_minutes)
        self._window_minutes = window_minutes
        self.merged_total = 0
        self.duplicates_total = 0

    def prime_from_db(self, now: dt.datetime) -> int:
        """Naplní okno z DB — po restartu se jinak duplikuje čerstvý sběr."""
        since = now - dt.timedelta(minutes=self._window_minutes)
        recent = self._writer.recent(since)
        self._dedup.prime(recent)
        if recent:
            logger.info("Dedup okno naplněno %d eventy z DB", len(recent))
        return len(recent)

    def write(self, events: Sequence[NewsEvent]) -> int:
        result = self._dedup.process(events)
        self.merged_total += result.merged
        self.duplicates_total += result.duplicates
        if not result.events:
            return 0
        # Zdroje, které tutéž story potvrdily, se ukládají k zapisovanému eventu:
        # latence per zdroj je podklad pro budoucí prioritizaci (SPEC 3.3)
        enriched = []
        for event in result.events:
            merged = self._dedup.merged_sources(event)
            if merged:
                event = NewsEvent(
                    **{**event.__dict__, "raw": {**event.raw, "merged_sources": merged}}
                )
            enriched.append(event)
        return self._writer.write(enriched)
