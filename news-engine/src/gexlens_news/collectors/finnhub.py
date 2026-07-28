"""Finnhub `/news` — hlavní headline zdroj (Tier B, SPEC kap. 1).

Free tier, klíč z `.env` (S10). Bez klíče se collector vůbec nezakládá —
vypnutý zdroj není porucha, takže se nemá hlásit jako degraded.

Odpověď je pole objektů s `datetime` (epoch sekundy), `headline`, `summary`,
`source`, `id`, `related`. Tvar není smluvní, takže parsování je defenzivní.
"""

import datetime as dt
import json
import logging
from collections.abc import Sequence
from typing import Any

from gexlens_news.collectors import CollectorClock, utc_now
from gexlens_news.http import Fetcher
from gexlens_news.model import NewsEvent, RawItem

logger = logging.getLogger(__name__)

BASE_URL = "https://finnhub.io/api/v1/news"
SUMMARY_LIMIT = 500


class FinnhubCollector:
    """Obecné tržní headliny à 1 min (SPEC kap. 1, Tier B)."""

    def __init__(
        self,
        api_key: str,
        fetcher: Fetcher,
        *,
        interval_s: float = 60.0,
        category: str = "general",
        base_url: str = BASE_URL,
        clock: CollectorClock = utc_now,
    ) -> None:
        if not api_key:
            raise ValueError("FinnhubCollector vyžaduje API klíč")
        self._api_key = api_key
        self._fetcher = fetcher
        self._interval_s = interval_s
        self._category = category
        self._base_url = base_url
        self._clock = clock

    @property
    def name(self) -> str:
        return "finnhub"

    @property
    def interval_s(self) -> float:
        return self._interval_s

    @property
    def _url(self) -> str:
        return f"{self._base_url}?category={self._category}&token={self._api_key}"

    async def fetch(self) -> Sequence[RawItem]:
        response = await self._fetcher.get(self._url)
        if response.not_modified:
            return []
        payload = json.loads(response.text)
        if not isinstance(payload, list):
            raise ValueError(f"Finnhub nevrátil seznam, ale {type(payload).__name__}")
        now = self._clock()
        return [
            RawItem(source=self.name, payload=entry, fetched_at=now)
            for entry in payload
            if isinstance(entry, dict)
        ]

    def normalize(self, item: RawItem) -> NewsEvent | None:
        payload: dict[str, Any] = item.payload
        title = str(payload.get("headline") or "").strip()
        if not title:
            return None
        ts_event = item.fetched_at
        raw_ts = payload.get("datetime")
        if isinstance(raw_ts, (int, float)) and raw_ts > 0:
            try:
                ts_event = dt.datetime.fromtimestamp(float(raw_ts), tz=dt.UTC)
            except (OverflowError, OSError, ValueError):
                logger.debug("Nečitelný timestamp %r u %r", raw_ts, title)
        summary = payload.get("summary")
        uid = payload.get("id")
        return NewsEvent(
            ts_event=ts_event,
            ts_ingested=item.fetched_at,
            source=self.name,
            source_uid=str(uid) if uid is not None else None,
            kind="headline",
            # Kategorii i směr doplní klasifikátor v N3 — Tier B je surový vstup
            category=None,
            importance=None,
            title=title,
            summary=str(summary)[:SUMMARY_LIMIT] if summary else None,
            symbols=[],
            raw=payload,
        )

    def sanitized_url(self) -> str:
        """URL bez tokenu — do logů a `raw` nesmí klíč prosáknout (S10)."""
        return f"{self._base_url}?category={self._category}&token=***"
