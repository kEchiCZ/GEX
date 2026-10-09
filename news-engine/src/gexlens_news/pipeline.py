"""Zápisová cesta: dedup → writer (#273).

Runner volá jediný `Writer` callable; tady se mezi normalizaci a databázi
vkládá rolling-window deduplikace, aby se tatáž story z více zdrojů zapsala
jednou a kopie z ostatních zdrojů se zaznamenaly v `news_event_sources`
(ADR-0059 bod 3; SPEC 3.1 pořadí: normalizer → dedup → writer).
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

    def prime_from_db(self, now: dt.datetime) -> int:
        """Naplní okno z DB — po restartu se jinak duplikuje čerstvý sběr."""
        since = now - dt.timedelta(minutes=self._window_minutes)
        recent = self._writer.recent(since)
        self._dedup.prime(recent)
        if recent:
            logger.info("Dedup okno naplněno %d eventy z DB", len(recent))
        return len(recent)

    def write(self, events: Sequence[NewsEvent]) -> int:
        """Zapíše nové eventy, pak kopie; vrací počet nových eventů.

        Kopie jdou až po eventech: první výskyt může být ve stejné dávce.
        """
        result = self._dedup.process(events)
        written = self._writer.write(result.events) if result.events else 0
        if result.copies:
            self._writer.write_copies(result.copies)
        return written
