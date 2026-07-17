"""Vstupní bod enginu: `python -m gexlens_engine` (SPEC kap. 8, headless u IB Gateway).

Sestaví produkční závislosti (ib_async, PostgreSQL, Parquet, HTTP publisher)
a spustí multi-instrument orchestrátor (ADR-0003): cílová sada podkladů =
GEXLENS_SYMBOLS + watchlist z DB, pipeline per instrument, sweepy sekvenčně.
Ranní OI archiv se doplňuje per instrument, noční retention purge běží globálně.
"""

import asyncio
import datetime as dt
import logging
import os
import sys

from ib_async import IB, Contract, Future, RealTimeBarList, Ticker
from sqlalchemy import create_engine

from gexlens_engine.adapters import HttpPublisher, IbOIFetcher, IbQuoteStreamer
from gexlens_engine.compute.cumdelta import CumDeltaTracker
from gexlens_engine.config import ConfigError, Settings, load_settings
from gexlens_engine.ibkr.connection import ConnectionManager, ConnectionState
from gexlens_engine.ibkr.discovery import ChainDiscovery, Underlying, build_contracts
from gexlens_engine.ibkr.scheduler import SubscriptionScheduler
from gexlens_engine.ibkr.underlying import Bar, RealTimeBarAggregator
from gexlens_engine.instruments import (
    SETUP_RETRY_CYCLES,
    InstrumentPipeline,
    InstrumentSetupError,
    WatchlistReader,
    aggregate_status,
    clamp_strike_range,
    expiry_expired,
    gather_metrics,
    merge_symbols,
    parse_multiplier,
    plan_instruments,
    read_watchlist,
)
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.storage.oi_archive import OIArchiver, OIEodRepository
from gexlens_engine.storage.parquet_store import SnapshotWriter
from gexlens_engine.storage.retention import RetentionJob

logger = logging.getLogger("gexlens.engine")

# Hlavní US futures burzy — filtr discovery podkladu (QBALGO apod. vynecháváme)
FUTURES_EXCHANGES = ("CME", "CBOT", "NYMEX", "COMEX")


async def _resolve_front_future(ib: IB, symbol: str) -> Contract:
    """Front futures kontrakt podkladu; timeout + omezený retry (sec-def farm výpadky)."""
    for attempt in range(3):
        try:
            details = await asyncio.wait_for(
                ib.reqContractDetailsAsync(Future(symbol, exchange="")), timeout=30.0
            )
        except TimeoutError:
            logger.warning("Discovery %s timeout (pokus %d/3)", symbol, attempt + 1)
            details = []
        contracts = [
            d.contract
            for d in details
            if d.contract is not None and d.contract.exchange in FUTURES_EXCHANGES
        ]
        if contracts:
            contracts.sort(key=lambda c: c.lastTradeDateOrContractMonth)
            return contracts[0]
        await asyncio.sleep(5)
    raise InstrumentSetupError(
        f"{symbol}: podklad nenalezen jako futures na {'/'.join(FUTURES_EXCHANGES)} "
        "(podporovány jsou futures opce — ADR-0003)"
    )


async def create_pipeline(
    ib: IB,
    manager: ConnectionManager,
    settings: Settings,
    publisher: PublisherLike,
    writer: SnapshotWriter,
    oi_repository: OIEodRepository,
    symbol: str,
) -> InstrumentPipeline:
    """Produkční sestavení pipeline jednoho podkladu nad ib_async."""
    front = await _resolve_front_future(ib, symbol)
    multiplier = parse_multiplier(front.multiplier)

    # Bary podkladu: 5s realtime bary → 1min agregace
    minute_bars: list[Bar] = []
    aggregator = RealTimeBarAggregator(minute_bars.append)

    def on_bar_update(bars: RealTimeBarList, has_new: bool) -> None:
        latest = bars[-1]
        aggregator.add_5s_bar(
            Bar(
                ts=latest.time,
                open=latest.open_,
                high=latest.high,
                low=latest.low,
                close=latest.close,
                volume=float(latest.volume),
            )
        )

    stopped = False
    rt_bars: RealTimeBarList | None = None

    def subscribe_underlying() -> Ticker:
        """Trvalé subskripce podkladu — při startu a po každém reconnectu.

        Reconnect zahazuje serverové subskripce; rotační sweep opcí se obnoví
        sám dalším cyklem, ale spot ticker a realtime bary jsou trvalé a bez
        obnovy by po prvním výpadku zamrzly (spot) a přestaly chodit (bary).
        """
        nonlocal rt_bars
        ticker = ib.reqMktData(front, "", False, False)
        bars_list = ib.reqRealTimeBars(front, 5, "TRADES", False)
        bars_list.updateEvent += on_bar_update
        rt_bars = bars_list
        return ticker

    fut_ticker = subscribe_underlying()
    await asyncio.sleep(3)
    spot = fut_ticker.last if fut_ticker.last == fut_ticker.last else fut_ticker.marketPrice()
    if spot != spot:
        ib.cancelMktData(front)
        raise InstrumentSetupError(f"{symbol}: nedorazila cena podkladu (subskripce dat?)")

    discovery = ChainDiscovery(ib, settings)
    underlying = Underlying(
        symbol=symbol, sec_type="FUT", exchange=front.exchange, con_id=front.conId
    )
    infos = await discovery.discover(underlying)
    if not infos:
        ib.cancelMktData(front)
        raise InstrumentSetupError(f"{symbol}: žádný FOP řetězec na {front.exchange}")
    info = infos[0]
    band = discovery.initial_band(info, spot)
    contracts = build_contracts(underlying, info, band)
    logger.info(
        "Řetězec %s %s %s: %d kontraktů, spot %.2f, multiplikátor %g",
        symbol,
        info.trading_class,
        info.expiry,
        len(contracts),
        spot,
        multiplier,
    )

    streamer = IbQuoteStreamer(ib)
    runtime = EngineRuntime(
        settings=settings,
        scheduler=SubscriptionScheduler(streamer, settings),
        writer=writer,
        oi_repository=oi_repository,
        publisher=publisher,
        symbol=symbol,
        expiry=info.expiry,
        multiplier=multiplier,
        contracts=contracts,
        cum_delta=CumDeltaTracker(multiplier=multiplier),
        push_status=False,  # agregovaný status pushuje orchestrátor
    )

    def on_stop() -> None:
        nonlocal stopped
        stopped = True
        ib.cancelMktData(front)
        if rt_bars is not None:
            ib.cancelRealTimeBars(rt_bars)

    pipeline = InstrumentPipeline(
        symbol=symbol,
        settings=settings,
        publisher=publisher,
        discovery=discovery,
        info=info,
        band=band,
        runtime=runtime,
        archiver=OIArchiver(oi_repository, IbOIFetcher(ib, streamer), settings),
        oi_repository=oi_repository,
        ticker=fut_ticker,
        minute_bars=minute_bars,
        on_stop=on_stop,
        spot=spot,
    )

    async def resubscribe() -> None:
        """Po reconnectu obnoví trvalé subskripce podkladu (spot + realtime bary)."""
        if stopped:
            return
        pipeline.ticker = subscribe_underlying()
        logger.info("Obnoveny subskripce podkladu %s po reconnectu", symbol)

    manager.on_resubscribe(resubscribe)

    pipeline.oi_available = await pipeline.try_archive_oi(dt.datetime.now(dt.UTC).date())
    return pipeline


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from exc

    api_base = os.environ.get("GEXLENS_API_BASE", "http://127.0.0.1:8000")
    publisher = HttpPublisher(api_base)
    ib = IB()
    manager = ConnectionManager(
        ib,
        settings,
        heartbeat_interval_s=settings.heartbeat_interval_s,
        heartbeat_timeout_s=settings.heartbeat_timeout_s,
    )
    await manager.start()
    while manager.state is not ConnectionState.CONNECTED:
        await asyncio.sleep(0.5)

    writer = SnapshotWriter(settings)
    db = create_engine(settings.database_url)
    oi_repository = OIEodRepository(db)
    await asyncio.to_thread(oi_repository.ensure_schema)
    watchlist_reader = WatchlistReader(db)
    await asyncio.to_thread(watchlist_reader.ensure_schema)

    retention = RetentionJob(settings)
    last_purge_date: dt.date | None = None

    pipelines: dict[str, InstrumentPipeline] = {}
    # Symboly po selhaném setupu: cooldown v cyklech do dalšího pokusu
    setup_cooldown: dict[str, int] = {}
    desired = merge_symbols(settings.symbol_list, await read_watchlist(watchlist_reader))
    cycle = 0

    while True:
        cycle_start = asyncio.get_running_loop().time()
        now = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0)

        # Watchlist se čte každý k-tý cyklus (uživatel přidal/odebral ticker v UI)
        if cycle % settings.watchlist_poll_cycles == 0:
            desired = merge_symbols(settings.symbol_list, await read_watchlist(watchlist_reader))
            # Runtime šířka pásma strikes ze Settings UI (vidět vzdálená křídla)
            override = await asyncio.to_thread(watchlist_reader.setting, "strike_range_points")
            new_range = clamp_strike_range(override, settings) if override is not None else None
            if new_range is not None:
                logger.info(
                    "Runtime změna rozsahu strikes: %g → %g bodů — pipeline se překlopí",
                    settings.strike_range_points,
                    new_range,
                )
                settings.strike_range_points = new_range
                for symbol in list(pipelines):
                    pipelines.pop(symbol).stop()

        # Denní roll expirace (0DTE): vypršelou pipeline zastavit — plán ji založí
        # znovu a discovery vybere novou nejbližší expiraci
        for symbol in list(pipelines):
            if expiry_expired(pipelines[symbol].runtime.expiry, now.date()):
                logger.info(
                    "Expirace %s pipeline %s vypršela — roll na novou",
                    pipelines[symbol].runtime.expiry,
                    symbol,
                )
                pipelines.pop(symbol).stop()

        for symbol in list(setup_cooldown):
            setup_cooldown[symbol] -= 1
            if setup_cooldown[symbol] <= 0:
                del setup_cooldown[symbol]
        eligible = [symbol for symbol in desired if symbol not in setup_cooldown]

        plan = plan_instruments(pipelines.keys(), eligible, settings.max_instruments)
        for symbol in plan.stop:
            logger.info("Zastavuji pipeline %s (odebráno z watchlistu)", symbol)
            pipelines.pop(symbol).stop()
        for symbol in plan.start:
            try:
                pipelines[symbol] = await create_pipeline(
                    ib, manager, settings, publisher, writer, oi_repository, symbol
                )
            except InstrumentSetupError as exc:
                setup_cooldown[symbol] = SETUP_RETRY_CYCLES
                logger.warning("Setup %s selhal: %s", symbol, exc)
                await publisher.publish(
                    "alerts",
                    {
                        "kind": "instrument_error",
                        "symbol": symbol,
                        "message": str(exc),
                        "ts": now.timestamp(),
                    },
                )
            except Exception:
                setup_cooldown[symbol] = SETUP_RETRY_CYCLES
                logger.exception("Setup %s selhal neočekávaně — cooldown", symbol)
        if plan.skipped:
            logger.warning(
                "Nad strop max_instruments=%d: %s neběží",
                settings.max_instruments,
                ",".join(plan.skipped),
            )

        # Sekvenční minutové cykly všech instrumentů + agregovaný status
        results = await gather_metrics(list(pipelines.values()), now)
        if results:
            await publisher.status(
                engine="online",
                connection="connected",
                port=settings.ibkr_port,
                last_tick_ts=now.isoformat(),
                **aggregate_status(results),
            )

        # Noční purge (jednou po konfigurovaném čase)
        if (
            dt.datetime.now(dt.UTC).time() >= settings.retention_purge_time_utc
            and last_purge_date != dt.datetime.now(dt.UTC).date()
        ):
            report = await asyncio.to_thread(retention.purge, dt.datetime.now(dt.UTC).date())
            last_purge_date = dt.datetime.now(dt.UTC).date()
            if report.disk_limit_exceeded:
                await publisher.publish(
                    "alerts",
                    {
                        "kind": "disk_limit",
                        "symbol": "*",
                        "message": "Disk limit překročen",
                        "ts": now.timestamp(),
                    },
                )

        cycle += 1
        elapsed = asyncio.get_running_loop().time() - cycle_start
        await asyncio.sleep(max(1.0, 60.0 - elapsed))


if __name__ == "__main__":
    asyncio.run(main())
