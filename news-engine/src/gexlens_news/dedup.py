"""Deduplikace a slučování zpráv napříč zdroji (#273, SPEC 3.3).

Rolling okno, **ne fixní časové buckety**: dvě znění téže story ve 13:59:58
a 14:00:01 by v bucketech spadla do různých a nesloučila se. Nový event se
proto porovnává proti všem eventům z posledních `window_minutes` — v paměti,
s doplněním z DB po startu.

Cross-source merge je smysl celé redundance zdrojů (SPEC kap. 1, Tier B):
tatáž zpráva z Finnhubu i CNBC má být **jeden** záznam, `source` nese ten
nejrychlejší a ostatní se schovají do `raw.merged_sources`. Naměřená latence
per zdroj je podklad pro budoucí prioritizaci.

Vědomé omezení: shoda je na normalizovaném titulku, takže přeformulovanou
story mezi zdroji nechytí. Fuzzy matching (simhash) je follow-up #274 —
ladění podobnostních prahů nemá blokovat N1.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from gexlens_news.model import NewsEvent, normalize_title

logger = logging.getLogger(__name__)

# Okno pro porovnání „je to tatáž story?" (SPEC 3.3 mluví o jednotkách minut;
# 10 min dává rezervu pomalejším RSS zdrojům, které tutéž zprávu vydají později)
DEFAULT_WINDOW_MINUTES = 10


@dataclass
class _Seen:
    """Záznam v okně: první výskyt story a zdroje, které ji potvrdily."""

    key: str
    ts_event: dt.datetime
    first_source: str
    first_ingested: dt.datetime
    merged: list[dict[str, object]] = field(default_factory=list)


@dataclass
class DedupResult:
    """Výsledek jedné dávky: co zapsat a co se slilo."""

    events: list[NewsEvent]
    merged: int = 0
    duplicates: int = 0


class RollingDeduplicator:
    """Drží okno nedávných stories a slučuje do nich nové výskyty."""

    def __init__(self, *, window_minutes: int = DEFAULT_WINDOW_MINUTES) -> None:
        self._window = dt.timedelta(minutes=window_minutes)
        self._seen: dict[str, _Seen] = {}

    @staticmethod
    def key_of(event: NewsEvent) -> str:
        """Klíč shody — normalizovaný titulek bez ohledu na zdroj a den.

        Den (na rozdíl od `dedup_hash`) záměrně **není** součástí: story přes
        půlnoc je pořád tatáž story a rolling okno ji má sloučit. Datum řeší
        až `dedup_hash` jako pojistka proti opakovanému fetchi.
        """
        return normalize_title(event.title)

    def _prune(self, now: dt.datetime) -> None:
        cutoff = now - self._window
        stale = [key for key, seen in self._seen.items() if seen.ts_event < cutoff]
        for key in stale:
            del self._seen[key]

    def prime(self, events: Sequence[NewsEvent]) -> None:
        """Naplní okno z DB po startu — jinak by se po restartu duplikovalo."""
        for event in events:
            key = self.key_of(event)
            self._seen.setdefault(
                key,
                _Seen(
                    key=key,
                    ts_event=event.ts_event,
                    first_source=event.source,
                    first_ingested=event.ts_ingested,
                ),
            )

    def process(self, events: Sequence[NewsEvent]) -> DedupResult:
        """Rozdělí dávku na nové eventy a slučované výskyty.

        Vrací jen ty, které se mají zapsat; u sloučených se do `raw` prvního
        výskytu nedostaneme (už je v DB), proto se merge loguje a promítá do
        `merged_sources` u eventu, který se právě zapisuje.
        """
        result = DedupResult(events=[])
        for event in sorted(events, key=lambda e: e.ts_event):
            self._prune(event.ts_event)
            key = self.key_of(event)
            seen = self._seen.get(key)
            if seen is None:
                self._seen[key] = _Seen(
                    key=key,
                    ts_event=event.ts_event,
                    first_source=event.source,
                    first_ingested=event.ts_ingested,
                )
                result.events.append(event)
                continue

            if seen.first_source == event.source:
                # Týž zdroj, tatáž story v okně = opakovaný fetch, ne nová zpráva
                result.duplicates += 1
                continue

            latency_s = (event.ts_ingested - seen.first_ingested).total_seconds()
            seen.merged.append(
                {
                    "source": event.source,
                    "source_uid": event.source_uid,
                    "ts_ingested": event.ts_ingested.isoformat(),
                    "latency_s": latency_s,
                }
            )
            result.merged += 1
            logger.debug(
                "Merge: %r už má %s, %s je o %.1f s pozdější",
                event.title,
                seen.first_source,
                event.source,
                latency_s,
            )
        return result

    def merged_sources(self, event: NewsEvent) -> list[dict[str, object]]:
        """Zdroje, které tutéž story potvrdily po prvním výskytu."""
        seen = self._seen.get(self.key_of(event))
        return list(seen.merged) if seen else []
