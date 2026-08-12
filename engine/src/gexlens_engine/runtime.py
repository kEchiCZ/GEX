"""Runtime enginu (SPEC kap. 2 + 8): slepení komponent do běžícího procesu.

Všechny závislosti jsou injektované (streamer kotací, OI fetcher, publisher do
API, writer) — runtime je tak testovatelný nad mocky (CLAUDE.md pravidlo 4)
a produkční adaptéry nad ib_async dodává `gexlens_engine.adapters`.

Jeden minutový cyklus: sweep řetězce → Parquet snapshot → GEX/levels →
CumΔ (bar větev) → flow → bary podkladu → push stavu a live kanálů do API.
"""

import asyncio
import datetime as dt
import functools
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from gexlens_engine.compute.cumdelta import CumDeltaTracker
from gexlens_engine.compute.flowoi import oi_estimate
from gexlens_engine.compute.gex import GexEngine, GexInput
from gexlens_engine.compute.gexfield import (
    GexProfile,
    ProfileContract,
    gamma_field,
    gamma_profile,
    greek_fields,
    greek_profiles,
)
from gexlens_engine.compute.levels import GexLevels, compute_ladder, compute_levels
from gexlens_engine.compute.marketclock import is_market_closed
from gexlens_engine.compute.settle import session_bounds, settle_ts, trading_session_date
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.scheduler import (
    GREEKS_SOURCE_COMPUTED,
    SubscriptionScheduler,
    SweepMetrics,
)
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.parquet_store import (
    CatchUpRow,
    FlowRowLike,
    GexFieldRow,
    GexProfileRow,
    GreeksSourceRow,
    LadderRow,
    Levels2Row,
    LevelsRow,
    NetFlowRow,
    OiEstRow,
    OiMissingRow,
    SnapshotRow,
    SnapshotWriter,
    WallDomRow,
    read_last_cum_delta,
    read_netflow_latest,
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
    # Per-symbol α flow-adjusted odhadu (#232): None = default z konfigurace
    # (flow_oi_alpha); ranní kalibrace fáze 2 ho nastavuje za běhu
    flow_alpha: float | None = None
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
    # Navázání kumulativního net objemu z partice netflow po restartu (#232)
    _netflow_seed_pending: bool = field(default=True, init=False)
    # Navázání CumΔ z flow partice po restartu uprostřed seance (#638)
    _flow_seed_pending: bool = field(default=True, init=False)
    # Throttle logu dopočtených Greeks (#547): loguje se jen změna počtu
    _computed_greeks_logged: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.cum_delta is None:
            self.cum_delta = CumDeltaTracker(multiplier=self.multiplier)

    def _session_partition_days(self, session_day: dt.date) -> list[dt.date]:
        """UTC dny partic, přes které se rozkládá Globex seance `session_day` (#638)."""
        start, _ = session_bounds(session_day)
        days = {start.date(), session_day}
        return sorted(days)

    async def _seed_cum_delta(self, session_day: dt.date) -> None:
        """Jednorázově naváže CumΔ z flow partice po restartu uprostřed seance (#638).

        Kumulativ je kotvený na open seance — restart ho dřív nulovat směl,
        teď se základ přečte z posledního zapsaného řádku AKTUÁLNÍ seance
        (večer D−1 + D, okno session_bounds). Bez řádku v okně (start nové
        seance) se nenavazuje nic — nula je správný základ.
        """
        if not self._flow_seed_pending:
            return
        self._flow_seed_pending = False
        start, end = session_bounds(session_day)
        paths = [
            self.settings.derived_dir / self.symbol / "flow" / f"{day.isoformat()}.parquet"
            for day in self._session_partition_days(session_day)
        ]
        base = await asyncio.to_thread(
            functools.partial(read_last_cum_delta, paths, start=start, end=end)
        )
        if base is None or base == 0.0:
            return
        tracker = self.cum_delta
        assert tracker is not None  # nastaven v __post_init__
        tracker.restore_cum(base)
        logger.info(
            "%s %s: CumΔ navázán z flow partice (%.0f) — restart uprostřed seance %s",
            self.symbol,
            self.expiry,
            base,
            session_day.isoformat(),
        )

    async def _seed_net_volume(self, session_day: dt.date) -> None:
        """Jednorázově naváže kumulativní net objem z partice netflow (#232).

        Restart enginu uprostřed dne dřív vynuloval FA odhad — tok naměřený
        dopoledne existoval jen v paměti. Partice netflow ho drží, takže se
        odhad po startu naváže tam, kde předchozí proces skončil. Klíče už
        naměřené po restartu mají přednost (restore_net_volume je setdefault).
        Od #638 jen řádky AKTUÁLNÍ seance — kumulativ předchozí seance ležící
        v téže UTC partici se přenést nesmí.
        """
        if not self._netflow_seed_pending:
            return
        self._netflow_seed_pending = False
        start, end = session_bounds(session_day)
        stored: dict[tuple[float, str], float] = {}
        for day in self._session_partition_days(session_day):
            path = (
                self.settings.derived_dir
                / self.symbol
                / self.expiry
                / "netflow"
                / f"{day.isoformat()}.parquet"
            )
            partial = await asyncio.to_thread(
                functools.partial(read_netflow_latest, path, start=start, end=end)
            )
            # Pozdější partice (vyšší UTC den) přepíše starší stav téhož klíče
            stored.update(partial)
        if not stored:
            return
        tracker = self.cum_delta
        assert tracker is not None  # nastaven v __post_init__
        by_key = {(spec.strike, spec.right): spec for spec in self.contracts}
        restored = {by_key[key]: net for key, net in stored.items() if key in by_key}
        tracker.restore_net_volume(restored)
        logger.info(
            "%s %s: kumulativní net objem obnoven z partice netflow (%d stran)",
            self.symbol,
            self.expiry,
            len(restored),
        )

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
        # Hranice Globex seance (#638): reset kumulativů PŘED zpracováním
        # minuty nové seance — tentýž okamžik, kdy se překlápí osa dne (#512).
        # První cyklus po startu jen zafixuje seanci; navázání řeší seed níže.
        session_day = trading_session_date(ts_min)
        if tracker.roll_session(session_day):
            self._netflow_seed_pending = False  # nová seance nemá co navazovat
            self._flow_seed_pending = False
            logger.info(
                "%s %s: Globex seance %s — CumΔ i net objem začínají od nuly (#638)",
                self.symbol,
                self.expiry,
                session_day.isoformat(),
            )
        elif not self.secondary:
            # Restart uprostřed seance: navázat CumΔ z flow partice (flow
            # zapisuje jen aktivní řetěz, sekundární kumulativ nepoužívá)
            await self._seed_cum_delta(session_day)
        now_mono = time.monotonic()
        max_age = self.settings.quote_max_age_s
        expired = 0

        # 1) Snapshot řádky (OI z ranního archivu — tick 588 intraday nechodí, ADR-0001)
        rows: list[SnapshotRow] = []
        gex_inputs: list[GexInput] = []
        gex_specs: list[OptionContractSpec] = []
        profile_contracts: list[ProfileContract] = []
        oi_missing: list[OiMissingRow] = []
        greeks_computed: list[GreeksSourceRow] = []
        for spec in self.contracts:
            cached = quotes.get(spec)
            if cached is None:
                continue
            snapshot = cached.snapshot
            age = cached.age_s(now_mono)
            # Vlastní BS dopočet místo TWS modelu (#547) — do vlastní řady,
            # ať jde v grafu i zpětně poznat, které Greeks nejsou měřené
            if cached.source == GREEKS_SOURCE_COMPUTED:
                greeks_computed.append(
                    GreeksSourceRow(ts_min=ts_min, strike=spec.strike, right=spec.right)
                )
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
            computed_keys = {(row.strike, row.right) for row in greeks_computed}
            message_rows: list[dict[str, object]] = []
            for row in rows:
                message_row: dict[str, object] = {
                    "strike": row.strike,
                    "right": row.right,
                    "oi": row.oi,
                    "volume": row.volume,
                    "delta": row.delta,
                    # Vega pro VEX módy (#201) — aditivní pole
                    "vega": row.vega,
                    "stale_age": row.stale_age,
                    # Midpoint pro P/C v prémiích (#469) — aditivní; None = bez kotace
                    "mid": (
                        (row.bid + row.ask) / 2
                        if row.bid is not None
                        and row.ask is not None
                        and row.bid > 0
                        and row.ask > 0
                        else None
                    ),
                }
                # Aditivní klíč (#547): Greeks jsou vlastní BS dopočet, ne TWS
                # model — posílá se jen když platí, běžný řádek nenafukuje
                if (row.strike, row.right) in computed_keys:
                    message_row["greeks_computed"] = True
                message_rows.append(message_row)
            snapshot_message: dict[str, object] = {
                "ts_min": ts_min.isoformat(),
                "rows": message_rows,
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
        # Dopočtené Greeks (#547) — vlastní řada po vzoru oimissing; dokud TWS
        # model dodává, řada nevznikne vůbec
        if greeks_computed:
            await asyncio.to_thread(
                self.writer.write_greeks_source, self.symbol, self.expiry, day, greeks_computed
            )
        # Throttle (#547): hlásit jen změnu počtu, ne stejnou větu každou minutu
        if len(greeks_computed) != self._computed_greeks_logged:
            self._computed_greeks_logged = len(greeks_computed)
            if greeks_computed:
                logger.warning(
                    "%s %s: %d striků s vlastními BS greeks — TWS model je nedodává",
                    self.symbol,
                    ts_min.isoformat(),
                    len(greeks_computed),
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

        # Flow-adjusted vrstva (ADR-0011, #222/#232): OI odhad = ranní OI +
        # α·čistý klasifikovaný objem (buy − sell z midpoint testu / Lee–Ready).
        # Jen aktivní řetěz — tok se měří jen tam; α = 0 vrstvu vypíná. Jediný
        # odhad (compute/flowoi.oi_estimate) sdílí FA levels, FA Dyn GEX
        # profil/pole i řady netflow/oiest — všechno je TENTÝŽ model.
        alpha = self.flow_alpha if self.flow_alpha is not None else self.settings.flow_oi_alpha
        fa_oi: dict[OptionContractSpec, float] = {}
        if not self.secondary and alpha > 0.0:
            # Po restartu uprostřed dne naváže kumulativ z partice netflow —
            # jinak by odhad začínal od nuly a zahodil celý dopolední tok
            await self._seed_net_volume(session_day)
            fa_oi = {
                spec: oi_estimate(inp.oi, tracker.net_volume(spec), alpha)
                for inp, spec in zip(gex_inputs, gex_specs, strict=True)
            }
            # Persistence netflow (#232): kumulativ dne per strana — vstup ranní
            # kalibrace α a zpětné validace směru (znaménko net vs. ΔOI)
            netflow_rows = [
                NetFlowRow(ts_min=ts_min, strike=spec.strike, right=spec.right, net_volume=net)
                for spec, net in sorted(
                    tracker.net_volumes().items(),
                    key=lambda item: (item[0].strike, item[0].right),
                )
                if net != 0.0
            ]
            if netflow_rows:
                await asyncio.to_thread(
                    self.writer.write_netflow, self.symbol, self.expiry, day, netflow_rows
                )
            # Řada oiest (#232): jen strany, kde se odhad liší od měřeného OI —
            # frontend při FA zdroji přepíše měřenou matici těmito buňkami
            oiest_rows = [
                OiEstRow(ts_min=ts_min, strike=spec.strike, right=spec.right, oi_est=fa_oi[spec])
                for inp, spec in zip(gex_inputs, gex_specs, strict=True)
                if fa_oi[spec] != inp.oi
            ]
            if oiest_rows:
                await asyncio.to_thread(
                    self.writer.write_oiest, self.symbol, self.expiry, day, oiest_rows
                )
                await self.publisher.publish(
                    f"oiest.{self.symbol}.{self.expiry}",
                    {
                        "ts_min": ts_min.isoformat(),
                        "rows": [
                            {"strike": row.strike, "right": row.right, "oi_est": row.oi_est}
                            for row in oiest_rows
                        ],
                    },
                )
            fa_inputs = [
                GexInput(strike=inp.strike, right=inp.right, gamma=inp.gamma, oi=fa_oi[spec])
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

            # FA Dyn GEX (#232): TENTÝŽ výpočet profilu/pole nad OI_est —
            # parametrizovaný vstup, žádná druhá implementace. Jen gamma
            # (charm/vanna FA nemá ve scope). Zapisuje se každou minutu i bez
            # rozdílu vůči měřené vrstvě, aby frontend měl souvislou FA řadu
            # od začátku dne (podklad forward-filluje poslední profil).
            if fa_oi:
                fa_contracts = [
                    replace(contract, oi=fa_oi[spec])
                    for contract, spec in zip(profile_contracts, gex_specs, strict=True)
                ]
                fa_profile = gamma_profile(
                    fa_contracts,
                    ts_min=ts_min,
                    settle=settle,
                    grid_start=strikes_sorted[0],
                    grid_stop=strikes_sorted[-1],
                    grid_step=strike_step / 2.0,
                    multiplier=self.multiplier,
                )
                fa_profile_row = GexProfileRow(
                    ts_min=ts_min,
                    grid_start=fa_profile.grid_start,
                    grid_step=fa_profile.grid_step,
                    values=[round(value, 1) for value in fa_profile.values],
                )
                await asyncio.to_thread(
                    self.writer.write_gexprofile,
                    self.symbol,
                    self.expiry,
                    day,
                    [fa_profile_row],
                    subdir="gexprofilefa",
                )
                await self.publisher.publish(
                    f"gexprofilefa.{self.symbol}.{self.expiry}",
                    {
                        "ts_min": ts_min.isoformat(),
                        "grid_start": fa_profile_row.grid_start,
                        "grid_step": fa_profile_row.grid_step,
                        "values": fa_profile_row.values,
                    },
                )
                fa_field = gamma_field(
                    fa_contracts,
                    ts_min=ts_min,
                    settle=settle,
                    grid_start=strikes_sorted[0],
                    grid_stop=strikes_sorted[-1],
                    grid_step=strike_step / 2.0,
                    multiplier=self.multiplier,
                )
                if fa_field is not None:
                    fa_flat = [round(value, 1) for column in fa_field.values for value in column]
                    fa_field_row = GexFieldRow(
                        ts_min=ts_min,
                        grid_start=fa_field.grid_start,
                        grid_step=fa_field.grid_step,
                        col_start=fa_field.col_start,
                        col_step_min=fa_field.col_step_min,
                        col_count=len(fa_field.values),
                        values=fa_flat,
                    )
                    await asyncio.to_thread(
                        self.writer.write_gexfield,
                        self.symbol,
                        self.expiry,
                        day,
                        fa_field_row,
                        subdir="gexfieldfa",
                    )
                    await self.publisher.publish(
                        f"gexfieldfa.{self.symbol}.{self.expiry}",
                        {
                            "ts_min": ts_min.isoformat(),
                            "grid_start": fa_field_row.grid_start,
                            "grid_step": fa_field_row.grid_step,
                            "col_start": fa_field_row.col_start.isoformat(),
                            "col_step_min": fa_field_row.col_step_min,
                            "col_count": fa_field_row.col_count,
                            "values": fa_field_row.values,
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
