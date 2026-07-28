"""Jednotná entita NewsEvent a surový vstup collectoru (SPEC S2, kap. 2.1).

Všechny zdroje se normalizují do stejného tvaru — jedna tabulka, stejná pole
pro plánovaný makro release i pro breaking headline. Výjimka jsou crowd data
(Tier C), která jsou kontinuální řada, ne událost, a mají vlastní tabulku
(SPEC 5.8).
"""

import datetime as dt
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

# Slova, která nenesou význam pro shodu titulků — dedup je má ignorovat,
# ať „Fed holds rates" a „The Fed holds rates" splynou (SPEC 3.3)
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "is",
        "of",
        "on",
        "the",
        "to",
        "with",
    }
)
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class RawItem:
    """Nezpracovaný záznam ze zdroje — payload se ukládá do `news_events.raw`.

    Collector si do `payload` dá, co potřebuje pro `normalize`; engine do něj
    nezasahuje kromě sanitizace tokenů (S10).
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
    symbols: list[str] = field(default_factory=list)
    forecast: float | None = None
    previous: float | None = None
    actual: float | None = None
    surprise_z: float | None = None
    market_closed: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def dedup_hash(self) -> str:
        """Hash normalizovaného titulku — základ deduplikace (SPEC 3.3).

        Záměrně **bez času**: časové okno řeší rolling porovnání v #273, ne hash.
        Fixní časové buckety by rozdělily tutéž story na hranici okna.
        """
        return hashlib.sha256(normalize_title(self.title).encode("utf-8")).hexdigest()


def normalize_title(title: str) -> str:
    """Titulek na kanonický tvar: bez diakritiky, interpunkce a stopslov."""
    folded = unicodedata.normalize("NFKD", title.casefold())
    ascii_only = "".join(ch for ch in folded if not unicodedata.combining(ch))
    words = _SPACES.sub(" ", _NON_WORD.sub(" ", ascii_only)).strip().split(" ")
    kept = [w for w in words if w and w not in _STOPWORDS]
    return " ".join(kept) if kept else " ".join(words)
