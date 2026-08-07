"""Push do WS kanálů přes interní endpoint API (#286, SPEC kap. 8).

Kanály jsou generické — `LiveHub` v API nic o zprávách neví, takže stačí
publikovat pod dohodnutými jmény:

* `news` — nový event po klasifikaci
* `sentiment.{sym}` — hodnota indexu a aktivní topicy
* `news.upcoming` — upozornění T−10 min před plánovaným high-impact eventem

Selhání pushe **nesmí zastavit sběr**: WS je pohodlí navíc, data jsou v DB tak
jako tak a UI si je při dalším dotazu načte.
"""

import datetime as dt
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Jak dlouho předem se hlásí plánovaný event (SPEC kap. 8, kanál news.upcoming)
UPCOMING_LEAD_MINUTES = 10
# Upozorňuje se jen na to, co trhem opravdu hne
UPCOMING_MIN_IMPORTANCE = 3


class NewsPublisher:
    """HTTP klient interního ingestu; při nedostupném API jen loguje."""

    def __init__(self, api_base: str, *, api_token: str = "", timeout_s: float = 5.0) -> None:
        # Interní ingest je za sdíleným tajemstvím (#542) — bez tokenu 401
        headers = {"X-GEXLens-Token": api_token} if api_token else {}
        self._client = httpx.AsyncClient(base_url=api_base, timeout=timeout_s, headers=headers)
        # Aby se stejný event nehlásil každý cyklus znovu
        self._announced: set[int] = set()

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        try:
            await self._client.post("/internal/publish", json={"channel": channel, "data": data})
        except httpx.HTTPError as exc:
            logger.warning("Publish %s do API selhal: %s", channel, exc)

    async def publish_news(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            await self.publish("news", event)

    async def publish_sentiment(
        self, symbol: str, value: float, topics: list[dict[str, Any]], ts: dt.datetime
    ) -> None:
        await self.publish(
            f"sentiment.{symbol}",
            {"ts": ts.isoformat(), "value": value, "topics": topics},
        )

    async def publish_upcoming(self, events: list[dict[str, Any]], now: dt.datetime) -> int:
        """Ohlásí plánované eventy v okně T−10 min; každý právě jednou."""
        announced = 0
        horizon = now + dt.timedelta(minutes=UPCOMING_LEAD_MINUTES)
        for event in events:
            event_id = int(event["id"])
            if event_id in self._announced:
                continue
            if int(event.get("importance") or 0) < UPCOMING_MIN_IMPORTANCE:
                continue
            ts_event = event["ts_event"]
            if isinstance(ts_event, str):
                ts_event = dt.datetime.fromisoformat(ts_event)
            if ts_event.tzinfo is None:
                ts_event = ts_event.replace(tzinfo=dt.UTC)
            if not (now < ts_event <= horizon):
                continue
            self._announced.add(event_id)
            await self.publish(
                "news.upcoming",
                {
                    "id": event_id,
                    "title": event["title"],
                    "ts_event": ts_event.isoformat(),
                    "importance": event.get("importance"),
                    "minutes_ahead": round((ts_event - now).total_seconds() / 60),
                },
            )
            announced += 1
        return announced

    async def close(self) -> None:
        await self._client.aclose()
