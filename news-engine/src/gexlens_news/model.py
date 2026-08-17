"""Jednotná entita NewsEvent a surový vstup collectoru (SPEC S2, kap. 2.1).

Všechny zdroje se normalizují do stejného tvaru — jedna tabulka, stejná pole
pro plánovaný makro release i pro breaking headline. Výjimka jsou crowd data
(Tier C), která jsou kontinuální řada, ne událost, a mají vlastní tabulku
(SPEC 5.8).
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from gexlens_engine.compute.marketclock import is_market_closed as _is_market_closed
from gexlens_engine.compute.newstext import dedup_hash as _dedup_hash
from gexlens_engine.compute.newstext import normalize_title


@dataclass(frozen=True)
class RawItem:
    """Nezpracovaný záznam ze zdroje — payload se ukládá do `news_events.raw`.

    Collector si do `payload` dá, co potřebuje pro `normalize`; engine do něj
    nezasahuje. Sanitizace tokenů (S10, #553) probíhá až na zápisu do DB —
    `sanitize_raw` v NewsWriter.write a v crowd insert cestě.
    """

    source: str
    payload: dict[str, Any]
    fetched_at: dt.datetime


@dataclass(frozen=True)
class NewsEvent:
    """Normalizovaná událost připravená k zápisu do `news_events`."""

    ts_event: dt.datetime
    ts_ingested: dt.datetime
    source: str
    kind: str
    title: str
    source_uid: str | None = None
    category: str | None = None
    importance: int | None = None
    summary: str | None = None
    # Plné znění článku (#743) — jen pokud ho zdroj dodá; do modelu jde
    # titulek + první odstavec, ne celý text
    body: str | None = None
    symbols: list[str] = field(default_factory=list)
    forecast: float | None = None
    previous: float | None = None
    actual: float | None = None
    surprise_z: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def market_closed(self) -> bool:
        """Byl trh v čase události zavřený (SPEC 2.2)?

        Počítá se, ne nastavuje: jako pole to byl default `False`, který žádný
        collector nepřepsal, takže sobotní titulek byl v DB uložený, jako by
        trh běžel (#339). Odvozeno od `ts_event` to nemá jak zapomenout.
        """
        return _is_market_closed(self.ts_event)

    @property
    def dedup_hash(self) -> str:
        """Klíč pro idempotentní zápis (SPEC 3.3).

        Implementace je sdílená s enginem (`compute.newstext`), protože broker
        headlines z ticku 292 zapisuje engine (#291) — dvě implementace by
        znamenaly tutéž story v DB dvakrát.
        """
        return _dedup_hash(self.title, self.ts_event)


__all__ = ["NewsEvent", "RawItem", "normalize_title"]
