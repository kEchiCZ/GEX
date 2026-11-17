"""Zápisová cesta: dedup → writer (#273).

Runner volá jediný `Writer` callable; tady se mezi normalizaci a databázi
vkládá rolling-window deduplikace, aby se tatáž story z více zdrojů zapsala
jednou a kopie z ostatních zdrojů se zaznamenaly v `news_event_sources`
(ADR-0059 bod 3; SPEC 3.1 pořadí: normalizer → dedup → writer).
"""

import datetime as dt
import logging
import threading
from collections.abc import Sequence

from gexlens_news.dedup import DEFAULT_WINDOW_MINUTES, RollingDeduplicator
from gexlens_news.ffcalendar import drop_rescheduled
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
        self._lock = threading.Lock()
        # Posuny kalendáře FF už zalogované (řádek, nový čas): feed je nese do konce týdne
        self._reported_moves: set[tuple[int, dt.datetime]] = set()

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
        Zámek drží dedup a zápis pohromadě: Alpaca zapisuje z vlákna
        (`asyncio.to_thread`), runner ze smyčky. Bez něj by kopie z jedné
        cesty mohla hledat první doručení z druhé ještě před jeho commitem
        a okno by se měnilo během průchodu (`_prune`, `_fuzzy_match`).
        """
        with self._lock:
            events = self._without_rescheduled(events)
            result = self._dedup.process(events)
            written = self._writer.write(result.events) if result.events else 0
            if result.copies:
                self._writer.write_copies(result.copies)
        return written

    def _without_rescheduled(self, events: Sequence[NewsEvent]) -> list[NewsEvent]:
        """Posunutý čas položky kalendáře FF není nový release (#1298, posun 1:1).

        Feed je celý týden, takže posun se pozná proti uloženým řádkům
        (`ffcalendar`). Uložený řádek zůstává s původním časem a actual mu doplní
        refresh z historické stránky stejným párováním.
        """
        kept, moves = drop_rescheduled(self._writer.engine, events)
        for row, event in moves:
            if (row.id, event.ts_event) in self._reported_moves:
                continue
            self._reported_moves.add((row.id, event.ts_event))
            logger.info(
                "Kalendář FF: %s posunut z %s na %s, řádek %d zůstává (#1298)",
                row.title,
                f"{row.ts_event:%Y-%m-%d %H:%M}",
                f"{event.ts_event.astimezone(dt.UTC):%H:%M}",
                row.id,
            )
        return kept
