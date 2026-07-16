"""SubscriptionScheduler (SPEC 3.3): rotační sweep opčního řetězce v dávkách.

Cyklus dávky: subskribuj → čekej na kompletní sadu (bid/ask/last/volume + Greeks)
nebo timeout → ulož do cache → odsubskribuj → další dávka. Kontrakty bez
kompletních dat jdou do repair fronty s retry; po vyčerpání pokusů jsou označeny
jako stale se stářím. ATM ± `atm_sweep_width` strikes se sweepuje každý cyklus,
křídla každý `wings_sweep_every`-tý cyklus.
"""

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuoteSnapshot:
    """Kompletní sada dat jednoho kontraktu z jedné subskripce (SPEC 3.3)."""

    bid: float
    ask: float
    last: float
    volume: float
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float


@dataclass
class CachedQuote:
    """Poslední kompletní data kontraktu; stale = poslední sweep je nedokázal obnovit."""

    snapshot: QuoteSnapshot
    updated_at: float
    stale: bool = False

    def age_s(self, now: float) -> float:
        return now - self.updated_at


@dataclass(frozen=True)
class SweepMetrics:
    """Metriky jednoho sweepu pro stavovou lištu (SPEC 3.7: Greeks X/Y, Repair, lines %)."""

    total: int
    greeks_complete: int
    repair_count: int
    stale_count: int
    lines_utilization: float
    sweep_duration_s: float


class QuoteStreamerLike(Protocol):
    """Zdroj kotací: subskribuje kontrakt, počká na kompletní sadu nebo timeout.

    Vrací None při nekompletních datech. Testy používají
    `gexlens_engine.ibkr.mock.MockQuoteStreamer`; produkční implementace nad
    ib_async reqMktData přijde se zapojením enginu.
    """

    async def fetch_quote(
        self, spec: OptionContractSpec, timeout_s: float
    ) -> QuoteSnapshot | None: ...


class SubscriptionScheduler:
    """Rotuje subskripce řetězce v dávkách a udržuje in-memory cache kotací."""

    def __init__(self, streamer: QuoteStreamerLike, settings: Settings) -> None:
        self._streamer = streamer
        self._settings = settings
        self._cache: dict[OptionContractSpec, CachedQuote] = {}
        self._stale: set[OptionContractSpec] = set()
        self._cycle = 0
        self.last_metrics: SweepMetrics | None = None

    @property
    def cycle(self) -> int:
        return self._cycle

    @property
    def stale_contracts(self) -> set[OptionContractSpec]:
        """Kontrakty, které poslední sweep nedokázal obnovit (pro UI a stale_age)."""
        return set(self._stale)

    def quote(self, spec: OptionContractSpec) -> CachedQuote | None:
        return self._cache.get(spec)

    def quotes(self) -> dict[OptionContractSpec, CachedQuote]:
        return dict(self._cache)

    async def sweep(self, contracts: Sequence[OptionContractSpec], spot: float) -> SweepMetrics:
        """Jeden kompletní sweep: výběr dle priority, dávky, repair, metriky."""
        start = time.monotonic()
        selected = self._select_contracts(contracts, spot)

        incomplete = await self._fetch_in_batches(selected)
        repair_count = len(incomplete)

        # Repair fronta: retry, max repair_max_attempts pokusů na kontrakt za sweep
        attempts_left = self._settings.repair_max_attempts
        while incomplete and attempts_left > 0:
            logger.info("Repair: retrying %d incomplete strikes", len(incomplete))
            incomplete = await self._fetch_in_batches(incomplete)
            attempts_left -= 1

        # Po vyčerpání pokusů: stale označení (stáří nese cache záznam, pokud existuje)
        for spec in incomplete:
            self._stale.add(spec)
            cached = self._cache.get(spec)
            if cached is not None:
                cached.stale = True

        self._cycle += 1
        metrics = SweepMetrics(
            total=len(selected),
            greeks_complete=len(selected) - len(incomplete),
            repair_count=repair_count,
            stale_count=len(incomplete),
            lines_utilization=min(
                1.0, self._settings.batch_size / self._settings.market_data_lines
            ),
            sweep_duration_s=time.monotonic() - start,
        )
        self.last_metrics = metrics
        return metrics

    def _select_contracts(
        self, contracts: Sequence[OptionContractSpec], spot: float
    ) -> list[OptionContractSpec]:
        """Priorita sweepu: ATM ± atm_sweep_width strikes vždy, křídla každý k-tý cyklus."""
        strikes = sorted({c.strike for c in contracts})
        if not strikes:
            return []
        atm_index = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
        low = max(0, atm_index - self._settings.atm_sweep_width)
        high = atm_index + self._settings.atm_sweep_width
        atm_strikes = set(strikes[low : high + 1])
        include_wings = self._cycle % self._settings.wings_sweep_every == 0
        return [c for c in contracts if include_wings or c.strike in atm_strikes]

    async def _fetch_in_batches(
        self, specs: Sequence[OptionContractSpec]
    ) -> list[OptionContractSpec]:
        """Stáhne kotace po dávkách batch_size; vrátí kontrakty bez kompletních dat."""
        incomplete: list[OptionContractSpec] = []
        batch_size = self._settings.batch_size
        for offset in range(0, len(specs), batch_size):
            batch = specs[offset : offset + batch_size]
            results = await asyncio.gather(*(self._fetch_one(spec) for spec in batch))
            now = time.monotonic()
            for spec, snapshot in zip(batch, results, strict=True):
                if snapshot is None:
                    incomplete.append(spec)
                else:
                    self._cache[spec] = CachedQuote(snapshot=snapshot, updated_at=now)
                    self._stale.discard(spec)
        return incomplete

    async def _fetch_one(self, spec: OptionContractSpec) -> QuoteSnapshot | None:
        try:
            return await self._streamer.fetch_quote(spec, self._settings.batch_timeout_s)
        except Exception:
            # Chyba streamu = nekompletní kontrakt; nesmí shodit celý sweep (SPEC kap. 8)
            logger.exception("fetch_quote selhal pro %s", spec)
            return None
