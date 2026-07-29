"""Deduplikace a slučování zpráv napříč zdroji (#273, SPEC 3.3).

Rolling okno, **ne fixní časové buckety**: dvě znění téže story ve 13:59:58
a 14:00:01 by v bucketech spadla do různých a nesloučila se. Nový event se
proto porovnává proti všem eventům z posledních `window_minutes` — v paměti,
s doplněním z DB po startu.

Cross-source merge je smysl celé redundance zdrojů (SPEC kap. 1, Tier B):
tatáž zpráva z Finnhubu i CNBC má být **jeden** záznam, `source` nese ten
nejrychlejší a ostatní se schovají do `raw.merged_sources`. Naměřená latence
per zdroj je podklad pro budoucí prioritizaci.

Fuzzy vrstva (#274): přeformulovanou story chytá token Jaccard ≥ 0.9 nad
týmž oknem. Simhash ze SPEC byl na provozních datech zamítnut — Hammingova
vzdálenost pravé a falešné páry neodděluje (ADR-0016). Fuzzy se nikdy
nepouští na `scheduled` eventy: „Durable Goods" vs „Core Durable Goods"
jsou dvě různé kalendářní události, ne reformulace.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from gexlens_news.model import NewsEvent, normalize_title

logger = logging.getLogger(__name__)

# Okno pro porovnání „je to tatáž story?" (#351, ADR-0017). SPEC 3.3 mluvil
# o 10 minutách (rezerva na rychlost zdrojů), jenže zdroje tutéž story
# REPUBLIKUJÍ s Δt 23 min – hodiny; týž den to zachytí `dedup_hash`
# (titulek+den), přes půlnoc UTC ale nic — měřeno ~19 propuštěných
# duplicit/den. 6 h chytá republikace a drží denní rubriky se stejným
# titulkem (Market Talk Roundup, Δt ≈ 24 h) oddělené.
DEFAULT_WINDOW_MINUTES = 360
# Práh fuzzy shody — měřeno 29. 7. 2026 na 1658 zprávách z 24 h provozu (#274):
# J ≥ 0.9 dalo 24 párů, všechny ručně ověřené pravé duplicity; pásmo 0.83–0.88
# už obsahuje falešné merge (různé firmy v šablonových titulcích). ADR-0016.
DEFAULT_JACCARD_THRESHOLD = 0.9
# Fuzzy jen pro volné titulky. Scheduled eventy jsou položky kalendáře — páry
# jako „Durable Goods" vs „Core Durable Goods" (J=0.83) jsou různé události.
FUZZY_KINDS = frozenset({"headline", "broker"})


@dataclass
class _Seen:
    """Záznam v okně: první výskyt story a zdroje, které ji potvrdily."""

    key: str
    ts_event: dt.datetime
    first_source: str
    first_ingested: dt.datetime
    kind: str
    tokens: frozenset[str]
    merged: list[dict[str, object]] = field(default_factory=list)


@dataclass
class DedupResult:
    """Výsledek jedné dávky: co zapsat a co se slilo."""

    events: list[NewsEvent]
    merged: int = 0
    duplicates: int = 0


class RollingDeduplicator:
    """Drží okno nedávných stories a slučuje do nich nové výskyty."""

    def __init__(
        self,
        *,
        window_minutes: int = DEFAULT_WINDOW_MINUTES,
        jaccard_threshold: float | None = DEFAULT_JACCARD_THRESHOLD,
    ) -> None:
        self._window = dt.timedelta(minutes=window_minutes)
        self._jaccard = jaccard_threshold
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
                    kind=event.kind,
                    tokens=frozenset(key.split()),
                ),
            )

    def _fuzzy_match(self, event: NewsEvent, key: str) -> _Seen | None:
        """Nejpodobnější story v okně s Jaccard ≥ prahu; None = žádná.

        Lineární průchod oknem je záměr: při 10min okně jde o desítky záznamů
        a i s okny v hodinách (#351) o stovky množinových průniků na event —
        levnější než údržba LSH indexu, který by se stejně po každém prune
        přestavoval.
        """
        if self._jaccard is None or event.kind not in FUZZY_KINDS:
            return None
        tokens = frozenset(key.split())
        if not tokens:
            return None
        best: _Seen | None = None
        best_similarity = 0.0
        for seen in self._seen.values():
            if seen.kind not in FUZZY_KINDS or not seen.tokens:
                continue
            similarity = len(tokens & seen.tokens) / len(tokens | seen.tokens)
            if similarity >= self._jaccard and similarity > best_similarity:
                best = seen
                best_similarity = similarity
        if best is not None:
            logger.debug("Fuzzy merge (J=%.2f): %r ~ %r", best_similarity, event.title, best.key)
        return best

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
            seen = self._seen.get(key) or self._fuzzy_match(event, key)
            if seen is None:
                self._seen[key] = _Seen(
                    key=key,
                    ts_event=event.ts_event,
                    first_source=event.source,
                    first_ingested=event.ts_ingested,
                    kind=event.kind,
                    tokens=frozenset(key.split()),
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
