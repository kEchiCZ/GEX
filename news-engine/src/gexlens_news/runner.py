"""Běh collectorů: vlastní interval per zdroj, izolace chyb, degraded stavy.

Každý collector jede ve své smyčce, takže pomalý nebo mrtvý zdroj nebrzdí
ostatní (SPEC 3.1 — „async task per zdroj, vlastní interval + rate limiter").
Výjimka z `fetch`/`normalize` se zaloguje, započítá do zdraví zdroje a smyčka
pokračuje — engine kvůli zdroji nikdy nepadá.
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Callable, Sequence

from gexlens_news.collectors import Collector, CollectorHealth
from gexlens_news.model import NewsEvent

logger = logging.getLogger(__name__)

# Podpis zapisovače: vrací počet skutečně zapsaných eventů (duplicity se
# zahazují na unikátním dedup_hash)
Writer = Callable[[Sequence[NewsEvent]], int]


class CollectorRunner:
    """Spouští collectory a drží jejich zdraví.

    `now` je injektovatelné kvůli testům; produkčně `dt.datetime.now(dt.UTC)`.
    """

    def __init__(
        self,
        collectors: Sequence[Collector],
        writer: Writer,
        *,
        now: Callable[[], dt.datetime] | None = None,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        self._collectors = list(collectors)
        self._writer = writer
        self._now = now or (lambda: dt.datetime.now(dt.UTC))
        self._sleep = sleep or asyncio.sleep
        self.health: dict[str, CollectorHealth] = {
            collector.name: CollectorHealth(name=collector.name) for collector in self._collectors
        }

    async def run_once(self, collector: Collector) -> int:
        """Jeden cyklus zdroje: fetch → normalize → zápis. Vrací počet zapsaných.

        Nikdy nevyhazuje — chyba se promítne do `health` a vrátí se 0.
        Selhání normalizace jedné položky nezahodí celou dávku: zbytek projde.
        """
        health = self.health[collector.name]
        try:
            items = await collector.fetch()
        except Exception as error:  # noqa: BLE001 — izolace zdroje je záměr
            health.record_failure(error)
            logger.warning(
                "Collector %s selhal (%dx po sobě, stav %s): %s",
                collector.name,
                health.consecutive_failures,
                health.state,
                health.last_error,
            )
            return 0

        events: list[NewsEvent] = []
        for item in items:
            try:
                event = collector.normalize(item)
            except Exception:
                logger.exception("Normalizace položky %s selhala — přeskakuji", collector.name)
                continue
            if event is not None:
                events.append(event)
                health.latencies_s.append((event.ts_ingested - event.ts_event).total_seconds())

        try:
            written = self._writer(events) if events else 0
        except Exception as error:  # noqa: BLE001 — DB výpadek nesmí zabít smyčku
            health.record_failure(error)
            logger.exception("Zápis eventů %s selhal", collector.name)
            return 0

        health.record_success(now=self._now(), items=len(items), written=written)
        return written

    async def run_forever(self, collector: Collector, *, stop: asyncio.Event) -> None:
        """Smyčka jednoho zdroje do zastavení; interval se při chybách prodlužuje."""
        while not stop.is_set():
            await self.run_once(collector)
            delay = collector.interval_s * self.health[collector.name].backoff_multiplier
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                continue

    async def run(self, *, stop: asyncio.Event) -> None:
        """Spustí všechny zdroje paralelně a čeká na zastavení."""
        if not self._collectors:
            logger.warning("Žádný collector není nakonfigurovaný — news-engine jen běží")
            await stop.wait()
            return
        await asyncio.gather(
            *(self.run_forever(collector, stop=stop) for collector in self._collectors)
        )

    def status(self) -> list[CollectorHealth]:
        """Zdraví všech zdrojů — degradované první, ať jsou v CLI vidět nahoře."""
        order = {"degraded": 0, "idle": 1, "ok": 2}
        return sorted(self.health.values(), key=lambda h: (order[h.state], h.name))
