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
from gexlens_engine.compute.gexfield import ProfileContract, gamma_field, gamma_profile
from gexlens_engine.compute.levels import compute_levels
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.scheduler import SubscriptionScheduler, SweepMetrics
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.parquet_store import (
    FlowRowLike,
    GexFieldRow,
    GexProfileRow,
    Levels2Row,
    LevelsRow,
    SnapshotRow,
    SnapshotWriter,
)

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
    # Sekundární řetěz (následující expirace): jen snapshots + levels —
    # flow/CumΔ a bary podkladu patří výhradně aktivní expiraci (per-symbol soubory)
    secondary: bool = False
    # Poslední spočtené hodnoty cyklu — čte je SetupEngine (ADR-0004)
    last_levels: LevelsRow | None = field(default=None, init=False)
    last_flow: FlowRowLike | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.cum_delta is None:
            self.cum_delta = CumDeltaTracker(multiplier=self.multiplier)

    async def run_cycle(
        self,
        ts_min: dt.datetime,
        spot: float,
        bars: Sequence[Bar],
        forming_bar: Bar | None = None,
    ) -> SweepMetrics:
        """Jeden kompletní minutový cyklus (volaný smyčkou nebo testem); vrací metriky sweepu.

        `forming_bar` je rozdělaná agregace minuty `ts_min` (ADR-0005) — publikuje se
        i zapisuje jako provizorní, aby nejnovější sloupec mřížky měl svíčku hned.
        """
        day = ts_min.date()
        metrics = await self.scheduler.sweep(self.contracts, spot)
        quotes = self.scheduler.quotes()
        stale = self.scheduler.stale_contracts
        tracker = self.cum_delta
        assert tracker is not None  # nastaven v __post_init__

        # 1) Snapshot řádky (OI z ranního archivu — tick 588 intraday nechodí, ADR-0001)
        rows: list[SnapshotRow] = []
        gex_inputs: list[GexInput] = []
        profile_contracts: list[ProfileContract] = []
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
            # Dyn GEX profil (ADR-0009): BS gamma nad uloženou IV per kontrakt
            profile_contracts.append(
                ProfileContract(
                    strike=spec.strike,
                    right=spec.right,
                    iv=snapshot.iv or 0.0,
                    oi=oi,
                )
            )
            # CumΔ bar větev (hot zóna má vlastní tick větev přes on_trade)
            if not self.secondary:
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
            # Inkrementální řez minuty pro živý append heatmapy (#127) — jen pole nutná
            # pro frontend grid/profil; jede pro aktivní i sekundární řetěz
            await self.publisher.publish(
                f"snapshot.{self.symbol}.{self.expiry}",
                {
                    "ts_min": ts_min.isoformat(),
                    "rows": [
                        {
                            "strike": row.strike,
                            "right": row.right,
                            "oi": row.oi,
                            "volume": row.volume,
                            "delta": row.delta,
                            "stale_age": row.stale_age,
                        }
                        for row in rows
                    ],
                },
            )

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
        # Sekundární zdi (ADR-0008) — vlastní řada, ať se nemění LEVELS_SCHEMA
        levels2_row = Levels2Row(
            ts_min=ts_min,
            call_wall_2=levels.call_wall_2,
            put_wall_2=levels.put_wall_2,
        )
        await asyncio.to_thread(
            self.writer.write_levels2, self.symbol, self.expiry, day, [levels2_row]
        )
        self.last_levels = levels_row

        # Dyn GEX profil (ADR-0009, #203): NetGEX přes cenovou mřížku obálky —
        # historie profilů je zároveň naměřený díl budoucího 2D pole
        strikes_sorted = sorted({spec.strike for spec in self.contracts})
        if len(strikes_sorted) >= 2 and profile_contracts:
            strike_step = min(
                b - a for a, b in zip(strikes_sorted, strikes_sorted[1:], strict=False) if b > a
            )
            settle = dt.datetime.strptime(self.expiry, "%Y%m%d").replace(
                hour=20, minute=0, tzinfo=dt.UTC
            )
            profile = gamma_profile(
                profile_contracts,
                ts_min=ts_min,
                settle=settle,
                grid_start=strikes_sorted[0],
                grid_stop=strikes_sorted[-1],
                grid_step=strike_step / 2.0,
                multiplier=self.multiplier,
            )
            profile_row = GexProfileRow(
                ts_min=ts_min,
                grid_start=profile.grid_start,
                grid_step=profile.grid_step,
                values=[round(value, 1) for value in profile.values],
            )
            await asyncio.to_thread(
                self.writer.write_gexprofile, self.symbol, self.expiry, day, [profile_row]
            )
            await self.publisher.publish(
                f"gexprofile.{self.symbol}.{self.expiry}",
                {
                    "ts_min": ts_min.isoformat(),
                    "grid_start": profile_row.grid_start,
                    "grid_step": profile_row.grid_step,
                    "values": profile_row.values,
                },
            )
            # Modelované pole budoucích sloupců (ADR-0009 fáze 2): drží se jen
            # poslední stav — minulé sloupce 2D módu skládá frontend z historie
            # profilů výše, budoucí z tohoto pole
            gexfield = gamma_field(
                profile_contracts,
                ts_min=ts_min,
                settle=settle,
                grid_start=strikes_sorted[0],
                grid_stop=strikes_sorted[-1],
                grid_step=strike_step / 2.0,
                multiplier=self.multiplier,
            )
            if gexfield is not None:
                flat = [round(value, 1) for column in gexfield.values for value in column]
                field_row = GexFieldRow(
                    ts_min=ts_min,
                    grid_start=gexfield.grid_start,
                    grid_step=gexfield.grid_step,
                    col_start=gexfield.col_start,
                    col_step_min=gexfield.col_step_min,
                    col_count=len(gexfield.values),
                    values=flat,
                )
                await asyncio.to_thread(
                    self.writer.write_gexfield, self.symbol, self.expiry, day, field_row
                )
                await self.publisher.publish(
                    f"gexfield.{self.symbol}.{self.expiry}",
                    {
                        "ts_min": ts_min.isoformat(),
                        "grid_start": field_row.grid_start,
                        "grid_step": field_row.grid_step,
                        "col_start": field_row.col_start.isoformat(),
                        "col_step_min": field_row.col_step_min,
                        "col_count": field_row.col_count,
                        "values": field_row.values,
                    },
                )

        # 3) FlowΔ/CumΔ minuta + 4) bary podkladu — jen aktivní expirace
        # (soubory jsou per symbol; sekundární řetěz by je duplikoval)
        if self.secondary:
            await self.publisher.publish(
                f"levels.{self.symbol}.{self.expiry}",
                {
                    "ts_min": ts_min.isoformat(),
                    "flip": levels.flip,
                    "call_wall": levels.call_wall,
                    "put_wall": levels.put_wall,
                    "centroid": levels.centroid,
                    "total_gex": levels.total_gex,
                    # Sekundární zdi (ADR-0008) — aditivní pole, starší klienti ignorují
                    "call_wall_2": levels.call_wall_2,
                    "put_wall_2": levels.put_wall_2,
                },
            )
            logger.info(
                "Cyklus %s %s (sekundární): %d snapshotů, greeks %d/%d",
                self.symbol,
                self.expiry,
                len(rows),
                metrics.greeks_complete,
                metrics.total,
            )
            return metrics
        flow_row = tracker.close_minute(ts_min)
        self.last_flow = flow_row
        await asyncio.to_thread(self.writer.write_flow, self.symbol, day, [flow_row])

        if bars:
            await asyncio.to_thread(self.writer.write_bars, self.symbol, day, bars)
        # Provizorní bar rozdělané minuty (ADR-0005) — zapisuje se až po finálních,
        # aby ho jejich upsert nepřepsal; patří-li jiné minutě, ignoruje se.
        provisional = forming_bar if forming_bar is not None and forming_bar.ts == ts_min else None
        if provisional is not None:
            await asyncio.to_thread(self.writer.write_bars, self.symbol, day, [provisional])

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
                # Sekundární zdi (ADR-0008) — aditivní pole, starší klienti ignorují
                "call_wall_2": levels.call_wall_2,
                "put_wall_2": levels.put_wall_2,
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

        # Plná OHLC minuty (#127) — frontend vykreslí svíčku, ne jen linku.
        # `last` ponecháno kvůli zpětné kompatibilitě starších konzumentů.
        async def publish_bar(bar: Bar, *, final: bool) -> None:
            await self.publisher.publish(
                f"price.{self.symbol}",
                {
                    "ts": bar.ts.isoformat(),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "last": bar.close,
                    # ADR-0005: rozdělaná minuta vs. uzavřený bar
                    "final": final,
                },
            )

        if bars:
            await publish_bar(bars[-1], final=True)
        if provisional is not None:
            await publish_bar(provisional, final=False)
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
