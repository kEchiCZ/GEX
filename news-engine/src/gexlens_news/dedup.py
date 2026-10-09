"""Deduplikace a slučování zpráv napříč zdroji (#273, SPEC 3.3).

Rolling okno, **ne fixní časové buckety**: dvě znění téže story ve 13:59:58
a 14:00:01 by v bucketech spadla do různých a nesloučila se. Nový event se
proto porovnává proti všem eventům z posledních `window_minutes` — v paměti,
s doplněním z DB po startu.

Cross-source merge je smysl celé redundance zdrojů (SPEC kap. 1, Tier B):
tatáž zpráva z Finnhubu i CNBC má být **jeden** záznam v `news_events`,
`source` nese první doručení. Kopie z jiného zdroje se nezahodí beze stopy:
výsledek ji vrací s `dedup_hash` prvního výskytu a zápis ji uloží do
`news_event_sources` (ADR-0059 bod 3) — efektivní tier a zpoždění per zdroj.
Kopie z téhož zdroje je opakovaný fetch, duplicita bez záznamu.

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
    """Záznam v okně: první výskyt story."""

    key: str
    ts_event: dt.datetime
    first_source: str
    first_ingested: dt.datetime
    kind: str
    tokens: frozenset[str]
    # Hash prvního výskytu: podle něj zápis kopie najde řádek v `news_events`
    dedup_hash: str


@dataclass(frozen=True)
class DedupCopy:
    """Kopie story z jiného zdroje a `dedup_hash` jejího prvního výskytu."""

    event: NewsEvent
    first_hash: str


@dataclass
class DedupResult:
    """Výsledek jedné dávky: co zapsat, kopie z jiných zdrojů a duplicity."""

    events: list[NewsEvent]
    copies: list[DedupCopy] = field(default_factory=list)
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
        """Naplní okno z DB po startu — jinak by se po restartu duplikovalo.

        Ze dvou řádků téhož klíče (přes půlnoc UTC = jiný `dedup_hash`) zůstane
        nejdřívější bez ohledu na pořadí z DB: kopie se přiřazuje k prvnímu
        doručení (ADR-0059 bod 3).
        """
        for event in events:
            key = self.key_of(event)
            current = self._seen.get(key)
            if current is not None and current.ts_event <= event.ts_event:
                continue
            self._seen[key] = _Seen(
                key=key,
                ts_event=event.ts_event,
                first_source=event.source,
                first_ingested=event.ts_ingested,
                kind=event.kind,
                tokens=frozenset(key.split()),
                dedup_hash=event.dedup_hash,
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
        """Rozdělí dávku na nové eventy, kopie z jiných zdrojů a duplicity.

        Nové eventy se zapisují do `news_events`. Kopie nesou `dedup_hash`
        prvního výskytu, protože ten už může být v DB z dřívější dávky nebo
        z doby před restartem (`prime`); zápis kopie ho podle hashe najde.
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
                    dedup_hash=event.dedup_hash,
                )
                result.events.append(event)
                continue

            if seen.first_source == event.source:
                # Týž zdroj, tatáž story v okně = opakovaný fetch, ne nová zpráva
                result.duplicates += 1
                continue

            result.copies.append(DedupCopy(event=event, first_hash=seen.dedup_hash))
            logger.debug(
                "Kopie: %r už má %s, %s je o %.1f s pozdější",
                event.title,
                seen.first_source,
                event.source,
                (event.ts_ingested - seen.first_ingested).total_seconds(),
            )
        return result
