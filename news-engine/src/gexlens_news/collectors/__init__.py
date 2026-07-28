"""Kontrakt collectoru a jeho zdravotní stav (SPEC 3.2).

Každý zdroj implementuje `fetch()` a `normalize()`. Chyba zdroje **nikdy
neshodí engine** — zdroj přejde do stavu `degraded` a je vidět v UI, obdoba
repair queue v GEXLens. To je tvrdý požadavek kap. 10 (Odolnost): free zdroje
padají běžně a modul na tom nesmí stát.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from gexlens_news.model import NewsEvent, RawItem

CollectorState = Literal["idle", "ok", "degraded"]

# Po kolika po sobě jdoucích selháních se zdroj označí za degraded. Jedno
# selhání je běžný provoz free API (timeout, 429) a nemá nic hlásit.
DEGRADED_AFTER_FAILURES = 3
# Strop exponenciálního backoffu, aby mrtvý zdroj nebušil každou minutu
MAX_BACKOFF_MULTIPLIER = 8


@runtime_checkable
class Collector(Protocol):
    """Zdroj zpráv. `fetch` smí vyhodit — runner to ustojí a označí degraded."""

    @property
    def name(self) -> str:
        """Identifikátor shodný s `news_events.source`."""
        ...

    @property
    def interval_s(self) -> float:
        """Cílová perioda sběru; runner ji při chybách prodlužuje backoffem."""
        ...

    async def fetch(self) -> Sequence[RawItem]: ...

    def normalize(self, item: RawItem) -> NewsEvent | None:
        """Mapování na jednotnou entitu; None = položka se přeskočí."""
        ...


@dataclass
class CollectorHealth:
    """Stav jednoho zdroje pro CLI status a UI (SPEC 3.2, kap. 10)."""

    name: str
    state: CollectorState = "idle"
    last_ok: dt.datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    items_total: int = 0
    events_written: int = 0
    # Naměřené latence zdroje (ts_ingested − ts_event) — podklad pro budoucí
    # prioritizaci zdrojů při cross-source merge (SPEC 3.3)
    latencies_s: list[float] = field(default_factory=list)

    def record_success(self, *, now: dt.datetime, items: int, written: int) -> None:
        self.state = "ok"
        self.last_ok = now
        self.last_error = None
        self.consecutive_failures = 0
        self.items_total += items
        self.events_written += written

    def record_failure(self, error: BaseException) -> None:
        self.consecutive_failures += 1
        self.last_error = f"{type(error).__name__}: {error}"
        if self.consecutive_failures >= DEGRADED_AFTER_FAILURES:
            self.state = "degraded"

    @property
    def backoff_multiplier(self) -> int:
        """Exponenciální prodloužení intervalu po selháních (1, 2, 4, 8, …)."""
        if self.consecutive_failures == 0:
            return 1
        return int(min(MAX_BACKOFF_MULTIPLIER, 2 ** (self.consecutive_failures - 1)))
