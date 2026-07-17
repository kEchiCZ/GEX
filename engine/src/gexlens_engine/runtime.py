"""Runtime enginu (SPEC kap. 2 + 8): slepení komponent do běžícího procesu.

Všechny závislosti jsou injektované (streamer kotací, OI fetcher, publisher do
API, writer) — runtime je tak testovatelný nad mocky (CLAUDE.md pravidlo 4)
a produkční adaptéry nad ib_async dodává `gexlens_engine.adapters`.

Jeden minutový cyklus: sweep řetězce → Parquet snapshot → GEX/levels →
CumΔ (bar větev) → flow → bary podkladu → push stavu a live kanálů do API.
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from gexlens_engine.compute.cumdelta import CumDeltaTracker
from gexlens_engine.compute.gex import GexEngine, GexInput
from gexlens_engine.compute.levels import compute_levels
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.scheduler import SubscriptionScheduler, SweepMetrics
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.parquet_store import LevelsRow, SnapshotRow, SnapshotWriter

logger = logging.getLogger(__name__)


class PublisherLike:
    """Push do API serveru (interní ingest): stav pipeline a live kanály."""

    async def status(self, **fields: object) -> None:
        raise NotImplementedError

    async def publish(self, channel: str, data: dict[str, object]) -> None:
        raise NotImplementedError


class NullPublisher(PublisherLike):
    """Bez API serveru (CLI režim) se stav jen loguje."""

    async def status(self, **fields: object) -> None:
        logger.info("status: %s", fields)

    async def publish(self, channel: str, data: dict[str, object]) -> None:
        logger.debug("publish %s: %s", channel, data)


@dataclass
class EngineRuntime:
    """Minutová smyčka nad již objevneným řetězcem kontraktů."""

    settings: Settings
    scheduler: SubscriptionScheduler
    writer: SnapshotWriter
    oi_repository: OIEodRepository
    publisher: PublisherLike
    symbol: str
    expiry: str
    multiplier: float
    contracts: Sequence[OptionContractSpec]
    gex_engine: GexEngine = field(default_factory=GexEngine)
    cum_delta: CumDeltaTracker | None = None
    # Multi-instrument orchestrátor pushuje agregovaný status sám (ADR-0003)
    push_status: bool = True

    def __post_init__(self) -> None:
        if self.cum_delta is None:
            self.cum_delta = CumDeltaTracker(multiplier=self.multiplier)

    async def run_cycle(
        self, ts_min: dt.datetime, spot: float, bars: Sequence[Bar]
    ) -> SweepMetrics:
        """Jeden kompletní minutový cyklus (volaný smyčkou nebo testem); vrací metriky sweepu."""
        day = ts_min.date()
        metrics = await self.scheduler.sweep(self.contracts, spot)
        quotes = self.scheduler.quotes()
        stale = self.scheduler.stale_contracts
        tracker = self.cum_delta
        assert tracker is not None  # nastaven v __post_init__

        # 1) Snapshot řádky (OI z ranního archivu — tick 588 intraday nechodí, ADR-0001)
        rows: list[SnapshotRow] = []
        gex_inputs: list[GexInput] = []
        for spec in self.contracts:
            cached = quotes.get(spec)
            if cached is None:
                continue
            snapshot = cached.snapshot
            oi = (
                self.oi_repository.get_oi(
                    spec.symbol, day, spec.strike, spec.right, expiry=spec.expiry
                )
                or 0.0
            )
            rows.append(
                SnapshotRow(
                    ts_min=ts_min,
                    strike=spec.strike,
                    right=spec.right,
                    bid=snapshot.bid,
                    ask=snapshot.ask,
                    last=snapshot.last,
                    volume=snapshot.volume,
                    iv=snapshot.iv,
                    delta=snapshot.delta,
                    gamma=snapshot.gamma,
                    theta=snapshot.theta,
                    vega=snapshot.vega,
                    oi=oi,
                    stale_age=999.0 if spec in stale else 0.0,
                )
            )
            gex_inputs.append(
                GexInput(strike=spec.strike, right=spec.right, gamma=snapshot.gamma, oi=oi)
            )
            # CumΔ bar větev (hot zóna má vlastní tick větev přes on_trade)
            tracker.add_bar(
                spec,
                cumulative_volume=snapshot.volume,
                last=snapshot.last,
                bid=snapshot.bid,
                ask=snapshot.ask,
                delta=snapshot.delta,
            )
        if rows:
            await asyncio.to_thread(self.writer.write_minute, self.symbol, self.expiry, day, rows)

        # 2) GEX + levels
        gex = self.gex_engine.compute(gex_inputs, spot=spot, multiplier=self.multiplier)
        levels = compute_levels(gex.net_by_strike(), spot=spot)
        levels_row = LevelsRow(
            ts_min=ts_min,
            flip=levels.flip,
            call_wall=levels.call_wall,
            put_wall=levels.put_wall,
            centroid=levels.centroid,
            total_gex=levels.total_gex,
        )
        await asyncio.to_thread(
            self.writer.write_levels, self.symbol, self.expiry, day, [levels_row]
        )

        # 3) FlowΔ/CumΔ minuta
        flow_row = tracker.close_minute(ts_min)
        await asyncio.to_thread(self.writer.write_flow, self.symbol, day, [flow_row])

        # 4) Bary podkladu
        if bars:
            await asyncio.to_thread(self.writer.write_bars, self.symbol, day, bars)

        # 5) Push do API: stav pipeline + live kanály
        if self.push_status:
            await self.publisher.status(
                engine="online",
                connection="connected",
                port=self.settings.ibkr_port,
                greeks_complete=metrics.greeks_complete,
                greeks_total=metrics.total,
                repair_count=metrics.stale_count,
                lines_utilization=metrics.lines_utilization,
                last_tick_ts=ts_min.isoformat(),
            )
        await self.publisher.publish(
            f"levels.{self.symbol}.{self.expiry}",
            {
                "ts_min": ts_min.isoformat(),
                "flip": levels.flip,
                "call_wall": levels.call_wall,
                "put_wall": levels.put_wall,
                "centroid": levels.centroid,
                "total_gex": levels.total_gex,
            },
        )
        await self.publisher.publish(
            f"flow.{self.symbol}",
            {
                "ts_min": ts_min.isoformat(),
                "flow_delta": flow_row.flow_delta,
                "cum_delta": flow_row.cum_delta,
            },
        )
        if bars:
            last_bar = bars[-1]
            await self.publisher.publish(
                f"price.{self.symbol}", {"ts": last_bar.ts.isoformat(), "last": last_bar.close}
            )
        logger.info(
            "Cyklus %s %s: %d snapshotů, greeks %d/%d, sweep %.1fs",
            self.symbol,
            ts_min.isoformat(),
            len(rows),
            metrics.greeks_complete,
            metrics.total,
            metrics.sweep_duration_s,
        )
        return metrics
