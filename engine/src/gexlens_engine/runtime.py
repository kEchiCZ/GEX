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
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from gexlens_engine.compute.cumdelta import CumDeltaTracker
from gexlens_engine.compute.gex import GexEngine, GexInput
from gexlens_engine.compute.gexfield import (
    GexProfile,
    ProfileContract,
    greek_fields,
    greek_profiles,
)
from gexlens_engine.compute.levels import GexLevels, compute_ladder, compute_levels
from gexlens_engine.compute.marketclock import is_market_closed
from gexlens_engine.compute.settle import settle_ts
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.scheduler import SubscriptionScheduler, SweepMetrics
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.parquet_store import (
    CatchUpRow,
    FlowRowLike,
    GexFieldRow,
    GexProfileRow,
    LadderRow,
    Levels2Row,
    LevelsRow,
    OiMissingRow,
    SnapshotRow,
    SnapshotWriter,
    WallDomRow,
)

logger = logging.getLogger(__name__)


def _levels_message(ts_min: dt.datetime, levels: GexLevels) -> dict[str, object]:
    """WS zpráva kanálu levels.* — nová pole jen aditivně, starší klienti je ignorují."""
    return {
        "ts_min": ts_min.isoformat(),
        "flip": levels.flip,
        "call_wall": levels.call_wall,
        "put_wall": levels.put_wall,
        "centroid": levels.centroid,
        "total_gex": levels.total_gex,
        # Sekundární zdi (ADR-0008)
        "call_wall_2": levels.call_wall_2,
        "put_wall_2": levels.put_wall_2,
        # Dominance zdí (ADR-0010, #223)
        "call_wall_dom": levels.call_wall_dom,
        "put_wall_dom": levels.put_wall_dom,
        "call_wall_2_dom": levels.call_wall_2_dom,
        "put_wall_2_dom": levels.put_wall_2_dom,
    }


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
    # Kompletní levels vč. dominance zdí (ADR-0010, #223) — LevelsRow je nenese
    last_gex_levels: GexLevels | None = field(default=None, init=False)
    # Poslední Dyn GEX profil (ADR-0009) — tendency (#350) z něj čte gammu v místě ceny
    last_profile: GexProfile | None = field(default=None, init=False)
    # Charm/vanna profily (#204) — tendency v2 (#397) z nich čte toky v místě ceny
    last_charm_profile: GexProfile | None = field(default=None, init=False)
    last_vanna_profile: GexProfile | None = field(default=None, init=False)
    # Catch-up flag (#518, ADR-0024): první úspěšný sweep po startu procesu se
    # označí, pokud v běžícím dni chybí předchozí minuty — kumulativy v něm
    # dohánějí celou dobu výpadku, ne jednu minutu
    _catch_up_pending: bool = field(default=True, init=False)

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
        tracker = self.cum_delta
        assert tracker is not None  # nastaven v __post_init__
        now_mono = time.monotonic()
        max_age = self.settings.quote_max_age_s
        expired = 0

        # 1) Snapshot řádky (OI z ranního archivu — tick 588 intraday nechodí, ADR-0001)
        rows: list[SnapshotRow] = []
        gex_inputs: list[GexInput] = []
        gex_specs: list[OptionContractSpec] = []
        profile_contracts: list[ProfileContract] = []
        oi_missing: list[OiMissingRow] = []
        for spec in self.contracts:
            cached = quotes.get(spec)
            if cached is None:
                continue
            snapshot = cached.snapshot
            age = cached.age_s(now_mono)
            archived_oi = self.oi_repository.get_oi(
                spec.symbol, day, spec.strike, spec.right, expiry=spec.expiry
            )
            # `None` = archiv strike nepokrývá (přibyl posunem pásma) nebo OI
            # nedodalo IBKR. Do výpočtů jde 0.0 jako dřív (nulové OI přispívá
            # nulou), ale strike se zapíše do vlastní řady — jinak by graf
            # tvrdil změřenou nulu tam, kde nikdo nic nezměřil (#465)
            if archived_oi is None:
                oi_missing.append(OiMissingRow(ts_min=ts_min, strike=spec.strike, right=spec.right))
            oi = archived_oi or 0.0
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
                    # Skutečné stáří kotace (#306), ne sentinel — heatmapa i řetěz
                    # už stale odlišit umí, jen dosud dostávaly jen 0/999
                    stale_age=age,
                )
            )
            # Zmrzlá kotace do výpočtů nesmí (#306): GEX, zdi, flip, Max Pain
            # i Dyn GEX profil se z ní počítaly, aniž by to šlo poznat. Řádek
            # zůstává v snapshotu se svým stářím — chybějící díra je poctivější
            # než tiše zkažený výpočet.
            if age > max_age:
                expired += 1
                continue
            gex_inputs.append(
                GexInput(strike=spec.strike, right=spec.right, gamma=snapshot.gamma, oi=oi)
            )
            gex_specs.append(spec)
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
        # Catch-up flag (#518, ADR-0024): první minuta se snapshoty po startu
        # procesu, které v běžícím dni předchází neměřená minuta s otevřeným
        # trhem. Kumulativní denní volume v ní srovnal první sweep sám, takže
        # přírůstkové odvozeniny ji musí brát jako první měřenou minutu dne.
        # Start na začátku dne (předchozí minuta patří jinému dni) ani na
        # otevření seance (trh byl zavřený) flag nedostane — nic nechybí.
        catch_up = False
        if rows and self._catch_up_pending:
            self._catch_up_pending = False
            previous_minute = ts_min - dt.timedelta(minutes=1)
            catch_up = previous_minute.date() == day and not is_market_closed(previous_minute)
        if rows:
            await asyncio.to_thread(self.writer.write_minute, self.symbol, self.expiry, day, rows)
            if catch_up:
                await asyncio.to_thread(
                    self.writer.write_catch_up,
                    self.symbol,
                    self.expiry,
                    day,
                    [CatchUpRow(ts_min=ts_min)],
                )
                logger.warning(
                    "%s %s: první sweep po startu uprostřed dne — minuta označena catch_up",
                    self.symbol,
                    ts_min.isoformat(),
                )
            # Inkrementální řez minuty pro živý append heatmapy (#127) — jen pole nutná
            # pro frontend grid/profil; jede pro aktivní i sekundární řetěz
            snapshot_message: dict[str, object] = {
                "ts_min": ts_min.isoformat(),
                "rows": [
                    {
                        "strike": row.strike,
                        "right": row.right,
                        "oi": row.oi,
                        "volume": row.volume,
                        "delta": row.delta,
                        # Vega pro VEX módy (#201) — aditivní pole
                        "vega": row.vega,
                        "stale_age": row.stale_age,
                    }
                    for row in rows
                ],
            }
            # Aditivní klíč (#518) — starší klienti ho ignorují; posílá se jen
            # když platí, běžná minuta zprávu nenafukuje
            if catch_up:
                snapshot_message["catch_up"] = True
            await self.publisher.publish(f"snapshot.{self.symbol}.{self.expiry}", snapshot_message)

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
        # Striky bez OI (#465) — vlastní řada; v běžný den nevznikne vůbec
        if oi_missing:
            await asyncio.to_thread(
                self.writer.write_oi_missing, self.symbol, self.expiry, day, oi_missing
            )
            logger.warning(
                "%s %s: %d striků bez OI v archivu — v grafu se označí jako bez dat",
                self.symbol,
                ts_min.isoformat(),
                len(oi_missing),
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
        # Dominance zdí (ADR-0010, #223) — vlastní řada, stejný důvod
        walldom_row = WallDomRow(
            ts_min=ts_min,
            call_wall_dom=levels.call_wall_dom,
            put_wall_dom=levels.put_wall_dom,
            call_wall_2_dom=levels.call_wall_2_dom,
            put_wall_2_dom=levels.put_wall_2_dom,
        )
        await asyncio.to_thread(
            self.writer.write_walldom, self.symbol, self.expiry, day, [walldom_row]
        )
        self.last_levels = levels_row
        self.last_gex_levels = levels

        # GEX žebřík (#244): top-N významných striků per strana — vlastní řada
        # (proměnný počet příček) + aditivní WS kanál
        ladder = compute_ladder(
            gex.net_by_strike(),
            spot=spot,
            top_n=self.settings.ladder_top_n,
            min_share=self.settings.ladder_min_share,
        )
        ladder_row = LadderRow(
            ts_min=ts_min,
            call_strikes=[entry.strike for entry in ladder if entry.side == "call"],
            call_shares=[entry.share for entry in ladder if entry.side == "call"],
            put_strikes=[entry.strike for entry in ladder if entry.side == "put"],
            put_shares=[entry.share for entry in ladder if entry.side == "put"],
        )
        await asyncio.to_thread(
            self.writer.write_ladder, self.symbol, self.expiry, day, [ladder_row]
        )
        await self.publisher.publish(
            f"ladder.{self.symbol}.{self.expiry}",
            {
                "ts_min": ts_min.isoformat(),
                "call_strikes": ladder_row.call_strikes,
                "call_shares": ladder_row.call_shares,
                "put_strikes": ladder_row.put_strikes,
                "put_shares": ladder_row.put_shares,
            },
        )

        # Flow-adjusted levels (ADR-0011, #222): OI odhad = ranní OI + α·čistý
        # klasifikovaný objem (buy − sell z midpoint testu / Lee–Ready). Jen
        # aktivní řetěz — tok se měří jen tam; α = 0 vrstvu vypíná. Odhad
        # nejde pod nulu (pozice nemůže být záporná).
        alpha = self.settings.flow_oi_alpha
        if not self.secondary and alpha > 0.0:
            fa_inputs = [
                GexInput(
                    strike=inp.strike,
                    right=inp.right,
                    gamma=inp.gamma,
                    oi=max(0.0, inp.oi + alpha * tracker.net_volume(spec)),
                )
                for inp, spec in zip(gex_inputs, gex_specs, strict=True)
            ]
            gex_fa = self.gex_engine.compute(fa_inputs, spot=spot, multiplier=self.multiplier)
            levels_fa = compute_levels(gex_fa.net_by_strike(), spot=spot)
            levelsfa_row = LevelsRow(
                ts_min=ts_min,
                flip=levels_fa.flip,
                call_wall=levels_fa.call_wall,
                put_wall=levels_fa.put_wall,
                centroid=levels_fa.centroid,
                total_gex=levels_fa.total_gex,
            )
            await asyncio.to_thread(
                self.writer.write_levelsfa, self.symbol, self.expiry, day, [levelsfa_row]
            )
            await self.publisher.publish(
                f"levelsfa.{self.symbol}.{self.expiry}",
                {
                    "ts_min": ts_min.isoformat(),
                    "flip": levels_fa.flip,
                    "call_wall": levels_fa.call_wall,
                    "put_wall": levels_fa.put_wall,
                    "centroid": levels_fa.centroid,
                    "total_gex": levels_fa.total_gex,
                },
            )

        # Dyn GEX profil (ADR-0009, #203): NetGEX přes cenovou mřížku obálky —
        # historie profilů je zároveň naměřený díl budoucího 2D pole
        strikes_sorted = sorted({spec.strike for spec in self.contracts})
        if len(strikes_sorted) >= 2 and profile_contracts:
            strike_step = min(
                b - a for a, b in zip(strikes_sorted, strikes_sorted[1:], strict=False) if b > a
            )
            # Settle dne expirace ze sdílené konvence (#511) — 16:00 ET,
            # tedy 20:00 UTC v létě a 21:00 UTC v zimě
            settle = settle_ts(dt.datetime.strptime(self.expiry, "%Y%m%d").date())
            # Gamma + charm + vanna jedním průchodem (#204) — sdílené d1/φ,
            # tři plochy nestojí trojnásobek. Gamma drží původní kanály/adresáře.
            profiles = greek_profiles(
                profile_contracts,
                ts_min=ts_min,
                settle=settle,
                grid_start=strikes_sorted[0],
                grid_stop=strikes_sorted[-1],
                grid_step=strike_step / 2.0,
                multiplier=self.multiplier,
            )
            self.last_profile = profiles["gamma"]
            self.last_charm_profile = profiles["charm"]
            self.last_vanna_profile = profiles["vanna"]
            for greek, profile in profiles.items():
                profile_row = GexProfileRow(
                    ts_min=ts_min,
                    grid_start=profile.grid_start,
                    grid_step=profile.grid_step,
                    values=[round(value, 1) for value in profile.values],
                )
                subdir = "gexprofile" if greek == "gamma" else f"{greek}profile"
                await asyncio.to_thread(
                    self.writer.write_gexprofile,
                    self.symbol,
                    self.expiry,
                    day,
                    [profile_row],
                    subdir=subdir,
                )
                await self.publisher.publish(
                    f"{subdir}.{self.symbol}.{self.expiry}",
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
            fields = greek_fields(
                profile_contracts,
                ts_min=ts_min,
                settle=settle,
                grid_start=strikes_sorted[0],
                grid_stop=strikes_sorted[-1],
                grid_step=strike_step / 2.0,
                multiplier=self.multiplier,
            )
            for greek, field in (fields or {}).items():
                flat = [round(value, 1) for column in field.values for value in column]
                field_row = GexFieldRow(
                    ts_min=ts_min,
                    grid_start=field.grid_start,
                    grid_step=field.grid_step,
                    col_start=field.col_start,
                    col_step_min=field.col_step_min,
                    col_count=len(field.values),
                    values=flat,
                )
                subdir = "gexfield" if greek == "gamma" else f"{greek}field"
                await asyncio.to_thread(
                    self.writer.write_gexfield,
                    self.symbol,
                    self.expiry,
                    day,
                    field_row,
                    subdir=subdir,
                )
                await self.publisher.publish(
                    f"{subdir}.{self.symbol}.{self.expiry}",
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
                f"levels.{self.symbol}.{self.expiry}", _levels_message(ts_min, levels)
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
            f"levels.{self.symbol}.{self.expiry}", _levels_message(ts_min, levels)
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
