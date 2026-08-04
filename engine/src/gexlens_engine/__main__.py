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
import time
from collections.abc import Callable

from ib_async import IB, Contract, Future, RealTimeBarList, Ticker
from sqlalchemy import create_engine

from gexlens_engine.adapters import (
    HttpPublisher,
    IbHistoricalClient,
    IbOIFetcher,
    IbQuoteStreamer,
)
from gexlens_engine.compute.cumdelta import CumDeltaTracker
from gexlens_engine.compute.setups import SetupParams
from gexlens_engine.config import ConfigError, Settings, load_settings
from gexlens_engine.ibkr.connection import (
    DELAYED_DATA_ERROR_CODES,
    ConnectionManager,
    ConnectionState,
)
from gexlens_engine.ibkr.discovery import ChainDiscovery, Underlying, build_contracts
from gexlens_engine.ibkr.newsticks import (
    NewsTickCollector,
    NewsTickLike,
    subscribe_broad_tape,
    tape_symbol,
)
from gexlens_engine.ibkr.pacing import PacingGuard
from gexlens_engine.ibkr.scheduler import SubscriptionScheduler
from gexlens_engine.ibkr.subscription import (
    NOT_SUBSCRIBED_ERROR_CODE,
    SubscriptionErrorAlert,
    SubscriptionErrorTracker,
    contract_label,
)
from gexlens_engine.ibkr.underlying import Bar, RealTimeBarAggregator, UnderlyingBackfiller
from gexlens_engine.instruments import (
    SETUP_RETRY_CYCLES,
    InstrumentPipeline,
    InstrumentSetupError,
    WatchlistReader,
    aggregate_status,
    expiry_expired,
    gather_metrics,
    merge_symbols,
    parse_multiplier,
    plan_instruments,
    read_watchlist,
)
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.runtime_settings import RUNTIME_SETTINGS, apply_runtime_settings
from gexlens_engine.setups import SetupEngine
from gexlens_engine.spot_stream import SpotStreamer
from gexlens_engine.storage.fa_validation import FaValidationRepository
from gexlens_engine.storage.notify import WatchlistListener
from gexlens_engine.storage.oi_archive import OIArchiver, OIEodRepository
from gexlens_engine.storage.parquet_store import SnapshotWriter
from gexlens_engine.storage.retention import RetentionJob
from gexlens_engine.storage.sentiment import ensure_sentiment_schema
from gexlens_engine.storage.setups_store import SetupsRepository
from gexlens_engine.storage.t6_store import T6Repository
from gexlens_engine.storage.tendency_store import TendencyRepository
from gexlens_engine.t6 import T6Collector
from gexlens_engine.tendency import TendencyEngine

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


def _watch_subscription_errors(
    ib: IB,
    manager: ConnectionManager,
    settings: Settings,
    publisher: PublisherLike,
    alert_enabled: Callable[[], bool],
) -> SubscriptionErrorTracker:
    """Zapojení `ib.errorEvent` (#417): delayed data → fail-fast, 354 → hlídání shluků.

    Do #417 se errorEvent neodebíral vůbec a `ConnectionManager.report_error` byl
    mrtvý kód. Rozdělení kódů je záměrné: delayed data znamenají, že celý engine
    počítá nad nespolehlivými Greeks (SPEC 3.1 fail-fast), zatímco error 354 se
    týká JEDNOHO requestu a v provozu chodí sporadicky i s platnou subskripcí —
    shodit kvůli němu stav spojení by z výpadku farmy udělalo trvalou chybu.
    """
    tracker = SubscriptionErrorTracker(
        threshold=settings.subscription_error_threshold,
        window_s=settings.subscription_error_window_s,
        cooldown_s=settings.subscription_error_cooldown_s,
    )

    async def publish(alert: SubscriptionErrorAlert) -> None:
        await publisher.publish(
            "alerts",
            {
                "kind": "subscription_error",
                "symbol": alert.symbol,
                "message": alert.message,
                "ts": dt.datetime.now(dt.UTC).timestamp(),
            },
        )

    def on_error(reqId: int, code: int, message: str, contract: object = None) -> None:
        if code in DELAYED_DATA_ERROR_CODES:
            manager.report_error(code, message)
            return
        if code != NOT_SUBSCRIBED_ERROR_CODE:
            return  # ostatní kódy loguje ib_async samo
        label = contract_label(contract)
        symbol = str(getattr(contract, "symbol", "") or "")
        alert = tracker.observe(label, symbol, now=time.monotonic())
        if alert is None:
            # Ojedinělý výskyt = přechodný výpadek farmy; sweep si kontrakt vezme příště
            logger.debug("IBKR error 354 (reqId %s): %s — %s", reqId, label, message)
            return
        logger.error(
            "TWS odmítla market data %d× za %g s: %s",
            alert.count,
            alert.window_s,
            ", ".join(alert.contracts),
        )
        if alert_enabled():
            asyncio.create_task(publish(alert))

    ib.errorEvent += on_error
    return tracker


async def _start_broker_news(
    ib: IB, manager: ConnectionManager, collector: NewsTickCollector, publisher: PublisherLike
) -> None:
    """Broad tape všech news providerů + okamžitý zápis příchozích headlines (#334).

    Odebírá se jednou na spojení, ne per symbol: páska providera není vázaná na
    podklad, takže druhá subskripce by jen zdvojila tytéž ticky.
    """

    def make_contract(provider: str) -> Contract:
        return Contract(secType="NEWS", exchange=provider, symbol=tape_symbol(provider))

    async def resubscribe_news() -> None:
        # Reconnect zahazuje serverové subskripce; bez obnovy by páska po prvním
        # výpadku tiše umlkla
        providers = [p.code for p in await ib.reqNewsProvidersAsync()]
        subscribe_broad_tape(ib.client, providers, make_contract=make_contract)

    async def store(tick: NewsTickLike, now: dt.datetime) -> None:
        try:
            written = await asyncio.to_thread(collector.write, [tick], now=now)
        except Exception:
            logger.exception("Okamžitý zápis headline selhal — dožene minutový cyklus")
            return
        # Push hned po zápisu (#335): čekat na klasifikaci v news-engine by
        # zprávu zdrželo o minuty, a syrový titulek je použitelný sám o sobě
        for stored in written:
            await publisher.publish("news", stored.as_news_row())

    def on_news_tick(tick: NewsTickLike) -> None:
        # Zápis do DB je blokující; z handleru se jen odpálí úloha, ať se
        # nebrzdí síťová smyčka ib_async
        asyncio.create_task(store(tick, dt.datetime.now(dt.UTC)))

    ib.tickNewsEvent += on_news_tick
    manager.on_resubscribe(resubscribe_news)
    await resubscribe_news()


async def create_pipeline(
    ib: IB,
    manager: ConnectionManager,
    settings: Settings,
    publisher: PublisherLike,
    writer: SnapshotWriter,
    oi_repository: OIEodRepository,
    symbol: str,
    setups_repository: SetupsRepository | None = None,
    tendency_repository: TendencyRepository | None = None,
    t6_repository: T6Repository | None = None,
    pacing_guard: PacingGuard | None = None,
    fa_repository: FaValidationRepository | None = None,
    news_ticks: NewsTickCollector | None = None,
) -> InstrumentPipeline:
    """Produkční sestavení pipeline jednoho podkladu nad ib_async."""
    front = await _resolve_front_future(ib, symbol)
    multiplier = parse_multiplier(front.multiplier)
    if pacing_guard is None:
        pacing_guard = PacingGuard()

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
    loop = asyncio.get_running_loop()
    # Živý spot (#128): throttlovaný publish spot.{symbol} z ticker.updateEvent (~5 Hz)
    spot_streamer = SpotStreamer(publisher, symbol)

    def on_spot_tick(ticker: Ticker) -> None:
        if stopped:
            return
        price = ticker.last if ticker.last == ticker.last else ticker.marketPrice()
        published = spot_streamer.sample(price, loop.time())
        if published is None:
            return
        loop.create_task(
            publisher.publish(
                f"spot.{symbol}",
                {"ts": dt.datetime.now(dt.UTC).isoformat(), "price": published},
            )
        )

    def subscribe_underlying() -> Ticker:
        """Trvalé subskripce podkladu — při startu a po každém reconnectu.

        Reconnect zahazuje serverové subskripce; rotační sweep opcí se obnoví
        sám dalším cyklem, ale spot ticker a realtime bary jsou trvalé a bez
        obnovy by po prvním výpadku zamrzly (spot) a přestaly chodit (bary).
        """
        nonlocal rt_bars
        # Headlines se NEodebírají tady: na futures je IBKR odmítá (#334),
        # jede se přes broad tape providerů v `main()`.
        ticker = ib.reqMktData(front, "", False, False)
        ticker.updateEvent += on_spot_tick
        bars_list = ib.reqRealTimeBars(front, 5, "TRADES", False)
        bars_list.updateEvent += on_bar_update
        rt_bars = bars_list
        return ticker

    fut_ticker = subscribe_underlying()
    await asyncio.sleep(3)
    # Spot: live cena → marketPrice → poslední závěrečná (víkend/zavřený trh,
    # jinak by pipeline nešla založit mimo obchodní hodiny)
    spot = next(
        (
            value
            for value in (fut_ticker.last, fut_ticker.marketPrice(), fut_ticker.close)
            if value == value
        ),
        float("nan"),
    )
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
    # OI archiv pokrývá N nejbližších expirací — ΔOI vs. včera potřebuje stejný
    # kontrakt archivovaný ve dvou dnech (0DTE řetěz jinak srovnání nemá)
    archive_contracts = [
        spec
        for extra in infos[: settings.oi_archive_expiries]
        for spec in build_contracts(underlying, extra, discovery.initial_band(extra, spot))
    ]
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
    # Následující expirace (čtení positioningu příští seance): sekundární runtime
    # sweepuje v nižší kadenci, píše jen snapshots + levels své expirace
    next_runtime: EngineRuntime | None = None
    if settings.sweep_next_expiry and len(infos) > 1:
        next_info = infos[1]
        next_contracts = build_contracts(
            underlying, next_info, discovery.initial_band(next_info, spot)
        )
        next_runtime = EngineRuntime(
            settings=settings,
            scheduler=SubscriptionScheduler(streamer, settings),
            writer=writer,
            oi_repository=oi_repository,
            publisher=publisher,
            symbol=symbol,
            expiry=next_info.expiry,
            multiplier=multiplier,
            contracts=next_contracts,
            cum_delta=CumDeltaTracker(multiplier=multiplier),
            push_status=False,
            secondary=True,
        )
        logger.info(
            "Sekundární řetěz %s %s %s: %d kontraktů (kadence 1/%d)",
            symbol,
            next_info.trading_class,
            next_info.expiry,
            len(next_contracts),
            settings.next_expiry_sweep_every,
        )

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

    # Historical backfill 1min barů (SPEC 3.6, #221): aktuální den + retention
    # okno při startu, jednodenní re-backfill po výpadku real-time streamu
    backfiller = UnderlyingBackfiller(IbHistoricalClient(ib, front), pacing_guard, settings)

    async def backfill_today() -> None:
        day = dt.datetime.now(dt.UTC).date()
        day_bars = await backfiller.backfill_day(symbol, day)
        if day_bars:
            await asyncio.to_thread(writer.write_bars, symbol, day, day_bars)
        logger.info("Re-backfill %s %s: %d barů", symbol, day, len(day_bars))

    async def initial_backfill() -> None:
        try:
            by_day = await backfiller.backfill(symbol, dt.datetime.now(dt.UTC).date())
        except Exception:
            logger.exception("Backfill barů %s selhal — svíčky jen z živého streamu", symbol)
            return
        for day, day_bars in by_day.items():
            if day_bars:
                await asyncio.to_thread(writer.write_bars, symbol, day, day_bars)
        logger.info(
            "Backfill %s: %d dní, %d barů",
            symbol,
            sum(1 for day_bars in by_day.values() if day_bars),
            sum(len(day_bars) for day_bars in by_day.values()),
        )

    backfill_task = asyncio.create_task(initial_backfill())

    def on_stop() -> None:
        nonlocal stopped
        stopped = True
        backfill_task.cancel()
        spot_streamer.stop()
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
        forming_bar=lambda: aggregator.current,
        on_stop=on_stop,
        spot=spot,
        archive_contracts=archive_contracts,
        next_runtime=next_runtime,
        backfill_today=backfill_today,
        fa_repository=fa_repository,
        setup_engine=(
            SetupEngine(
                symbol=symbol,
                repository=setups_repository,
                oi_repository=oi_repository,
                publisher=publisher,
                params=SetupParams(
                    min_wall_dominance=settings.setup_min_wall_dominance,
                    counter_flow_lookback=settings.setup_counter_flow_lookback,
                    counter_stop_cooldown_minutes=settings.setup_counter_stop_cooldown_minutes,
                    disabled_templates=settings.setup_disabled_template_set,
                    min_risk_atr=settings.setup_min_risk_atr,
                    max_rr=settings.setup_max_rr,
                    max_stops_per_direction=settings.setup_max_stops_per_direction,
                    direction_block_minutes=settings.setup_direction_block_minutes,
                ),
            )
            if setups_repository is not None
            else None
        ),
        tendency_engine=(
            TendencyEngine(
                symbol=symbol,
                repository=tendency_repository,
                oi_repository=oi_repository,
                publisher=publisher,
                data_dir=settings.data_dir,
            )
            if tendency_repository is not None
            else None
        ),
        t6_collector=(
            T6Collector(
                symbol=symbol,
                repository=t6_repository,
                oi_repository=oi_repository,
                publisher=publisher,
                data_dir=settings.data_dir,
                trigger_pct=settings.t6_trigger_pct,
            )
            if t6_repository is not None
            else None
        ),
        news_ticks=news_ticks,
        read_news_ticks=(lambda: list(ib.newsTicks())) if news_ticks else None,
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
    # Alert subscription_error jde vypnout v Settings UI (#417); hodnota se
    # obnovuje v hlavním cyklu, handler errorEvent nesmí sahat do DB
    subscription_alerts = {"enabled": True}
    subscription_errors = _watch_subscription_errors(
        ib, manager, settings, publisher, lambda: subscription_alerts["enabled"]
    )
    await manager.start()
    while manager.state is not ConnectionState.CONNECTED:
        await asyncio.sleep(0.5)

    writer = SnapshotWriter(settings)
    db = create_engine(settings.database_url)
    oi_repository = OIEodRepository(db)
    await asyncio.to_thread(oi_repository.ensure_schema)
    # Denní FA validace (#232): body open-ratio se sbírají samy po OI archivu
    fa_repository = FaValidationRepository(db)
    await asyncio.to_thread(fa_repository.ensure_schema)
    watchlist_reader = WatchlistReader(db)
    await asyncio.to_thread(watchlist_reader.ensure_schema)
    # LISTEN na změny watchlistu (#207): nový symbol startuje do sekund;
    # poll à WATCHLIST_POLL_CYCLES zůstává jako fallback
    watchlist_listener = WatchlistListener(settings.database_url)
    watchlist_listener.start()
    setups_repository: SetupsRepository | None = None
    if settings.setups_enabled:
        setups_repository = SetupsRepository(db)
        await asyncio.to_thread(setups_repository.ensure_schema)
    tendency_repository: TendencyRepository | None = None
    if settings.tendency_enabled:
        tendency_repository = TendencyRepository(db)
        await asyncio.to_thread(tendency_repository.ensure_schema)
    t6_repository: T6Repository | None = None
    if settings.t6_collector_enabled:
        t6_repository = T6Repository(db)
        await asyncio.to_thread(t6_repository.ensure_schema)

    # Broker headlines z ticku 292 (#291): schéma SentimentLensu sdílí obě
    # služby, engine do něj jen zapisuje
    news_ticks: NewsTickCollector | None = None
    if settings.ibkr_news_enabled:
        await asyncio.to_thread(ensure_sentiment_schema, db)
        news_ticks = NewsTickCollector(db)
        await _start_broker_news(ib, manager, news_ticks, publisher)

    retention = RetentionJob(settings)
    last_purge_date: dt.date | None = None
    # Globální rate limiter historical requestů (SPEC 3.6) — sdílený všemi pipeline
    pacing_guard = PacingGuard()

    pipelines: dict[str, InstrumentPipeline] = {}
    # Symboly po selhaném setupu: cooldown v cyklech do dalšího pokusu
    setup_cooldown: dict[str, int] = {}
    desired = merge_symbols(settings.symbol_list, await read_watchlist(watchlist_reader))
    cycle = 0
    force_watchlist = False
    last_full_minute: dt.datetime | None = None

    while True:
        cycle_start = asyncio.get_running_loop().time()
        now = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0)

        # Watchlist se čte každý k-tý cyklus (uživatel přidal/odebral ticker v UI)
        # nebo hned po NOTIFY probuzení (#207)
        if force_watchlist or cycle % settings.watchlist_poll_cycles == 0:
            force_watchlist = False
            desired = merge_symbols(settings.symbol_list, await read_watchlist(watchlist_reader))
            # Nastavení laditelná za běhu ze Settings UI (#438) — jedním dotazem.
            # Do #438 se četl jen rozsah strikes; retence, disk limit, velikost
            # dávky a hot zóna se uložily, ale engine je nikdy nepřečetl.
            keys = [spec.key for spec in RUNTIME_SETTINGS] + ["subscription_alert_enabled"]
            stored = await asyncio.to_thread(watchlist_reader.settings_map, keys)
            subscription_alerts["enabled"] = stored.get("subscription_alert_enabled") is not False
            if apply_runtime_settings(settings, stored):
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
                    ib,
                    manager,
                    settings,
                    publisher,
                    writer,
                    oi_repository,
                    symbol,
                    setups_repository=setups_repository,
                    tendency_repository=tendency_repository,
                    t6_repository=t6_repository,
                    pacing_guard=pacing_guard,
                    fa_repository=fa_repository,
                    news_ticks=news_ticks,
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

        # Sekvenční minutové cykly všech instrumentů + agregovaný status.
        # NOTIFY probuzení uprostřed minuty (#207): plný cyklus téže minuty by
        # duplikoval zápisy (snapshoty se appendují) — běží jen nové pipeline,
        # status se pushuje jen z plného běhu (agregát přes všechny instrumenty).
        full_run = now != last_full_minute
        if full_run:
            run_list = list(pipelines.values())
            last_full_minute = now
        else:
            run_list = [pipelines[symbol] for symbol in plan.start if symbol in pipelines]
        results = await gather_metrics(run_list, now)
        if results and full_run:
            await publisher.status(
                engine="online",
                connection="connected",
                port=settings.ibkr_port,
                last_tick_ts=now.isoformat(),
                # Kolikrát TWS za běh odmítla market data (#417) — s platnými
                # subskripcemi má zůstat na nule, růst je signál k prověření
                subscription_errors=subscription_errors.total,
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
        # Místo sleep čekání na NOTIFY (#207) — změna watchlistu probudí smyčku hned
        if await watchlist_listener.wait(max(1.0, 60.0 - elapsed)):
            force_watchlist = True
            logger.info("Watchlist NOTIFY — okamžité přeplánování instrumentů")


if __name__ == "__main__":
    asyncio.run(main())
