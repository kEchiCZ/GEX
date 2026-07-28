"""ForexFactory kalendář — plánované makro události (Tier A, ADR-0013).

Nejčistší trénovací signál modulu: čas releasu je znám dopředu, takže reakci
trhu jde měřit bez dohadování, kdy zpráva dorazila.

Vědomá omezení zdroje (změřeno v ADR-0013):

* feed nese **jen aktuální týden** (`nextweek` vrací 404),
* **nemá pole `actual`** ani u proběhlých eventů — hodnoty doplní BLS/BEA/FRED
  mapováním řad v N2, tenhle collector zakládá event s forecast/previous,
* eventy nemají ID → `source_uid` se skládá z (country, title, date).

Formát není oficiálně garantovaný, proto je parsování defenzivní: nečitelná
položka se přeskočí a nezabije celou dávku (SPEC 3.2).
"""

import datetime as dt
import logging
import re
from collections.abc import Sequence
from typing import Any

from gexlens_news.collectors import CollectorClock, utc_now
from gexlens_news.http import Fetcher
from gexlens_news.model import NewsEvent, RawItem

logger = logging.getLogger(__name__)

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Mapa měn na dotčené symboly: ES/NQ jsou US indexy, takže reagují primárně
# na USD data. EUR/GBP/JPY zprávy se sbírají (risk sentiment se přelévá), ale
# symboly nedostávají — reakční okna by měřila šum.
_SYMBOLS_BY_COUNTRY = {"USD": ["ES", "NQ"]}

_IMPACT = {"high": 3, "medium": 2, "low": 1}

# Kategorie z názvu eventu. Pořadí rozhoduje — první shoda vyhrává, takže
# specifičtější vzory musí být dřív (FOMC před obecným "rate").
_CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("FED", re.compile(r"fomc|federal funds|fed chair|beige book|powell", re.I)),
    ("MACRO_INFLATION", re.compile(r"\bcpi\b|\bppi\b|pce|inflation|price index", re.I)),
    ("MACRO_LABOR", re.compile(r"non-farm|nonfarm|payroll|unemployment|jobless|employment", re.I)),
    ("MACRO_GROWTH", re.compile(r"\bgdp\b|retail sales|pmi|ism|durable goods|industrial", re.I)),
    ("ENERGY", re.compile(r"crude oil|natural gas|opec", re.I)),
)


def classify_title(title: str) -> str:
    """Kategorie plánovaného eventu z názvu (SPEC kap. 4 — scheduled LLM nepotřebuje)."""
    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.search(title):
            return category
    return "OTHER"


def parse_number(raw: object) -> float | None:
    """Číslo z hodnoty kalendáře (`3.4%`, `-1.2K`, `86.1`); nečitelné → None."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text or text in {"-", "—"}:
        return None
    multiplier = 1.0
    if text[-1] in "KMB":
        multiplier = {"K": 1e3, "M": 1e6, "B": 1e9}[text[-1]]
        text = text[:-1]
    text = text.rstrip("%")
    try:
        return float(text) * multiplier
    except ValueError:
        return None


class ForexFactoryCollector:
    """Týdenní kalendář; `interval_s` z konfigurace (default 1×/h)."""

    def __init__(
        self,
        fetcher: Fetcher,
        *,
        interval_s: float = 3600.0,
        url: str = FEED_URL,
        clock: CollectorClock = utc_now,
    ) -> None:
        self._fetcher = fetcher
        self._interval_s = interval_s
        self._url = url
        self._clock = clock

    @property
    def name(self) -> str:
        return "forexfactory"

    @property
    def interval_s(self) -> float:
        return self._interval_s

    async def fetch(self) -> Sequence[RawItem]:
        import json

        response = await self._fetcher.get(self._url)
        if response.not_modified:
            return []
        payload = json.loads(response.text)
        if not isinstance(payload, list):
            raise ValueError(f"Kalendář nemá tvar seznamu, ale {type(payload).__name__}")
        now = self._clock()
        return [
            RawItem(source=self.name, payload=entry, fetched_at=now)
            for entry in payload
            if isinstance(entry, dict)
        ]

    def normalize(self, item: RawItem) -> NewsEvent | None:
        payload: dict[str, Any] = item.payload
        title = str(payload.get("title") or "").strip()
        raw_date = payload.get("date")
        if not title or not raw_date:
            return None
        try:
            # ISO s explicitním offsetem (US Eastern vč. DST) — převod na UTC
            ts_event = dt.datetime.fromisoformat(str(raw_date)).astimezone(dt.UTC)
        except ValueError:
            logger.debug("Nečitelné datum %r u %r — přeskakuji", raw_date, title)
            return None

        country = str(payload.get("country") or "").upper()
        impact = _IMPACT.get(str(payload.get("impact") or "").lower(), 1)
        return NewsEvent(
            ts_event=ts_event,
            ts_ingested=item.fetched_at,
            source=self.name,
            # Titulek sám o sobě není unikátní (stejný název každý měsíc),
            # proto se do uid přidává čas releasu
            source_uid=f"{country}|{title}|{ts_event.isoformat()}",
            kind="scheduled",
            category=classify_title(title),
            importance=impact,
            title=f"{country} {title}" if country else title,
            summary=None,
            symbols=list(_SYMBOLS_BY_COUNTRY.get(country, [])),
            forecast=parse_number(payload.get("forecast")),
            previous=parse_number(payload.get("previous")),
            # Feed `actual` nenese (ADR-0013) — doplní oficiální API v N2
            actual=None,
            raw=payload,
        )
