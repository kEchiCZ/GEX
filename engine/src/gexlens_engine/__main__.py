"""Vstupní bod enginu: `python -m gexlens_engine` (SPEC kap. 8, headless u IB Gateway).

Sestaví produkční závislosti (ib_async, PostgreSQL, Parquet, HTTP publisher)
a spustí multi-instrument orchestrátor (ADR-0003): cílová sada podkladů =
GEXLENS_SYMBOLS + watchlist z DB, pipeline per instrument, sweepy sekvenčně.
Ranní OI archiv se doplňuje per instrument, noční retention purge běží globálně.
"""

import asyncio
import contextlib
import datetime as dt
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import Any

from ib_async import IB, Contract, Future, RealTimeBarList, Ticker
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from gexlens_engine.adapters import (
    HttpPublisher,
    IbkrProvider,
    count_ib_lines,
)
from gexlens_engine.compute.cumdelta import CumDeltaTracker
from gexlens_engine.compute.futures_cvd import FuturesCvdTracker
from gexlens_engine.compute.setups import SetupParams
from gexlens_engine.config import ConfigError, Settings, load_settings
from gexlens_engine.diagnostics import install_stack_dump
from gexlens_engine.gammacliff import GammaCliffCollector
from gexlens_engine.ibkr.account import classify_accounts
from gexlens_engine.ibkr.connection import (
    COMPETING_SESSION_ERROR_CODE,
    DELAYED_DATA_ERROR_CODES,
    ConnectionManager,
    ConnectionState,
)
from gexlens_engine.ibkr.discovery import (
    ChainDiscovery,
    ExpiryInfo,
    OptionContractSpec,
    StrikeBand,
    Underlying,
    build_contracts,
)
from gexlens_engine.ibkr.lines import LineGauge
from gexlens_engine.ibkr.newsticks import (
    ArticleFetcher,
    NewsTickCollector,
    NewsTickLike,
    broad_tape_providers,
    subscribe_broad_tape,
    tape_symbol,
)
from gexlens_engine.ibkr.pacing import PacingGuard
from gexlens_engine.ibkr.probe import FarmProbe
from gexlens_engine.ibkr.scheduler import CachedQuote, SubscriptionScheduler
from gexlens_engine.ibkr.subscription import (
    NOT_SUBSCRIBED_ERROR_CODE,
    SubscriptionErrorAlert,
    SubscriptionErrorTracker,
    contract_label,
)
from gexlens_engine.ibkr.underlying import Bar, RealTimeBarAggregator, UnderlyingBackfiller
from gexlens_engine.instruments import (
    InstrumentPipeline,
    InstrumentSetupError,
    SetupCooldown,
    WatchlistReader,
    aggregate_status,
    expiry_expired,
    gather_metrics,
    merge_symbols,
    parse_multiplier,
    plan_instruments,
    read_watchlist,
)
from gexlens_engine.provider import MarketDataProviderLike
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.runtime_settings import (
    CONNECTION_SETTINGS,
    RUNTIME_SETTINGS,
    apply_connection_settings,
    apply_runtime_settings,
)
from gexlens_engine.setups import SetupEngine
from gexlens_engine.spot_stream import SpotStreamer
from gexlens_engine.storage.diskwatch import DiskWatch, utcnow_ts
from gexlens_engine.storage.fa_calibration import FaAlphaRepository
from gexlens_engine.storage.fa_validation import FaValidationRepository
from gexlens_engine.storage.feed_comparison import FeedComparisonRepository
from gexlens_engine.storage.gammacliff_store import GammaCliffRepository
from gexlens_engine.storage.notify import WatchlistListener
from gexlens_engine.storage.oi_archive import OIArchiver, OIEodRepository
from gexlens_engine.storage.parquet_store import SnapshotWriter
from gexlens_engine.storage.retention import RetentionJob
from gexlens_engine.storage.sentiment import ensure_sentiment_schema
from gexlens_engine.storage.setups_store import SetupsRepository
from gexlens_engine.storage.t6_store import T6Repository
from gexlens_engine.storage.tendency_store import TendencyRepository
from gexlens_engine.storage.volregime_store import VolRegimeRepository
from gexlens_engine.t6 import T6Collector, recompute_stale_candidates
from gexlens_engine.tasty.chain_fallback import ChainFallback, tasty_chain_quotes
from gexlens_engine.tasty.crosscheck import CrossCheckDetector, CrossCheckVerdict
from gexlens_engine.tasty.devrun import run_tasty_only
from gexlens_engine.tasty.extended import (
    build_snapshot_rows,
    cadence_due,
    extended_streamers,
    plan_extended_expiries,
    validate_disjoint,
)
from gexlens_engine.tasty.greeks_validator import GreeksAlert, GreeksValidator
from gexlens_engine.tasty.monitor import MAX_AGE_MS, FeedMonitor, tracked_symbols
from gexlens_engine.tasty.provider import TastyChainCache
from gexlens_engine.tasty.session import TastyCredentials, TastySession
from gexlens_engine.tasty.spot_fallback import SpotFallback
from gexlens_engine.tasty.stream import DxLinkStream
from gexlens_engine.tasty.symbols import ChainSymbols, SymbolMap
from gexlens_engine.tasty.trades_recorder import TradesRecorder
from gexlens_engine.tendency import TendencyEngine
from gexlens_engine.volregime import VolRegimeCollector

logger = logging.getLogger("gexlens.engine")

#: Jak často se kontroluje mlčící IBKR spot (#614). Musí být výrazně kratší
#: než práh výpadku, jinak by se fallback zapínal se zpožděním celé periody.
SPOT_FALLBACK_POLL_S = 5.0

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
    # Konkurenční relace (#451) má vlastní počítadlo A vlastní práh (#495):
    # 10197 chodí ~2× za minutu, takže sdílený práh 5/60 s nešel nikdy naplnit
    # a alert se neodpálil. Hlásit ji každou minutu by ale bylo k ničemu —
    # proto delší cooldown.
    competing_sessions = SubscriptionErrorTracker(
        threshold=settings.competing_session_threshold,
        window_s=settings.competing_session_window_s,
        cooldown_s=max(settings.subscription_error_cooldown_s, 3600.0),
    )
    # `create_task` bez držené reference může GC uklidit před doběhem (#499,
    # RUF006) — alert by pak tiše nedorazil. Reference se drží do dokončení.
    pending_publishes: set[asyncio.Task[None]] = set()

    def spawn_publish(coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        pending_publishes.add(task)
        task.add_done_callback(pending_publishes.discard)

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

    async def publish_competing(alert: SubscriptionErrorAlert) -> None:
        await publisher.publish(
            "alerts",
            {
                "kind": "competing_session",
                "symbol": alert.symbol,
                "message": (
                    f"Stejný IBKR účet je přihlášený jinde a přetahuje si market data "
                    f"({alert.count}× za {alert.window_s:g} s). Data můžou vypadávat — "
                    "odhlas účet z mobilní aplikace, Client Portal nebo druhé TWS."
                ),
                "ts": dt.datetime.now(dt.UTC).timestamp(),
            },
        )

    def on_error(reqId: int, code: int, message: str, contract: object = None) -> None:
        if code in DELAYED_DATA_ERROR_CODES:
            manager.report_error(code, message)
            return
        if code == COMPETING_SESSION_ERROR_CODE:
            # Konkurenční relace (#451): stav spojení se NEMĚNÍ — data můžou téct
            # dál. Uživatel se to ale dozvědět má, protože při horším průběhu
            # feed mizí úplně (4. 8. tak vypadla data ve 14 cyklech ze 192).
            # Symbol se předává (#495) — alert v UI je vázaný na instrument.
            alert = competing_sessions.observe(
                contract_label(contract),
                str(getattr(contract, "symbol", "") or ""),
                now=time.monotonic(),
            )
            if alert is not None:
                logger.warning("Konkurenční relace odebírá market data: %s", message)
                if alert_enabled():
                    spawn_publish(publish_competing(alert))
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
            spawn_publish(publish(alert))

    ib.errorEvent += on_error
    return tracker


def _watch_connection_stall(
    manager: ConnectionManager, settings: Settings, publisher: PublisherLike
) -> None:
    """Dlouhý výpadek IBKR spojení do zvonečku (#770).

    Watchdog v ConnectionManageru hlásí přes `on_stall` každých
    `reconnect_stall_alert_s`, dokud se spojení nevrátí — 18. 8. byl engine
    osm hodin offline a poznalo se to jen tím, že si člověk všiml zamrzlého
    grafu. Log ERROR píše watchdog sám; tady se výpadek jen publikuje.
    """
    # RUF006: create_task bez držené reference může GC uklidit před doběhem
    # (#499) — alert by pak tiše nedorazil
    pending: set[asyncio.Task[None]] = set()

    def on_stall(offline_s: float) -> None:
        task = asyncio.create_task(
            publisher.publish(
                "alerts",
                {
                    "kind": "connection_stall",
                    "symbol": "*",
                    "message": (
                        f"IBKR spojení chybí už {offline_s / 60:.0f} min — sběr dat "
                        f"stojí. Zkontroluj TWS a API port "
                        f"{settings.ibkr_host}:{settings.ibkr_port}."
                    ),
                    "ts": dt.datetime.now(dt.UTC).timestamp(),
                },
            )
        )
        pending.add(task)
        task.add_done_callback(pending.discard)

    manager.on_stall(on_stall)


def _connection_offline_status(manager: ConnectionManager) -> dict[str, object]:
    """Délka výpadku IBKR spojení do /status (#770).

    Stejná konvence jako feed_crosscheck (#517 A): klíč CHYBÍ, když spojení
    drží — nepřítomnost znamená „nic k hlášení", ne „neměří se". Posílá se
    doba, ne timestamp (zdroj je monotonic, viz ConnectionManager.offline_for_s).
    """
    offline_for = manager.offline_for_s
    if offline_for is None:
        return {}
    return {"connection_offline_for_s": round(offline_for)}


async def _start_broker_news(
    ib: IB,
    manager: ConnectionManager,
    collector: NewsTickCollector,
    publisher: PublisherLike,
    article_fetcher: ArticleFetcher | None = None,
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
        codes = [p.code for p in await ib.reqNewsProvidersAsync()]
        # Kódy z reqNewsProviders jsou pro článkové API; broad tape zná jen
        # kořeny (#546) — bez normalizace pět requestů spadne a `DJ` chybí
        providers = broad_tape_providers(codes)
        if providers != codes:
            logger.info(
                "Broker news: %d kódů z IBKR → %d pásek (%s)",
                len(codes),
                len(providers),
                ", ".join(providers),
            )
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
        # Plné znění článku (#743) až PO pushi — titulek nesmí čekat na fetch
        if article_fetcher is not None and written:
            await article_fetcher.fetch_for(written)

    store_tasks: set[asyncio.Task[None]] = set()

    def _report_store(task: asyncio.Task[None]) -> None:
        store_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # Bez callbacku by pád zápisu (např. restart PG) skončil jen
            # „Task exception was never retrieved" při GC — tichá ztráta
            logger.error("Zápis broker headline selhal: %r", task.exception())

    def on_news_tick(tick: NewsTickLike) -> None:
        # Zápis do DB je blokující; z handleru se jen odpálí úloha, ať se
        # nebrzdí síťová smyčka ib_async. Reference se drží (RUF006).
        task = asyncio.create_task(store(tick, dt.datetime.now(dt.UTC)))
        store_tasks.add(task)
        task.add_done_callback(_report_store)

    ib.tickNewsEvent += on_news_tick
    manager.on_resubscribe(resubscribe_news)
    # Engine smí startovat bez TWS (#756) — eager odběr by na nepřipojeném
    # klientovi spadl ConnectionError a vzal celý main() s sebou (#778: proces
    # pak zůstal viset a kontejner vypadal zdravě). Zárukou odběru je registrace
    # výš: `on_resubscribe` běží po KAŽDÉM úspěšném (re)connectu; eager volání
    # je jen zkratka pro start s už běžícím TWS.
    if manager.state is not ConnectionState.CONNECTED:
        logger.info("IBKR nepřipojen — broker news se odebere s prvním connectem")
        return
    try:
        await resubscribe_news()
    except ConnectionError:
        # Spojení spadlo mezi kontrolou stavu a odběrem — obnoví ho reconnect
        logger.warning("Odběr broker news selhal (spojení spadlo) — obnoví se po reconnectu")


def _crosscheck_status(detector: CrossCheckDetector | None) -> dict[str, object]:
    """Pole křížové kontroly do /status (#517 A).

    Bez shadow větve (nebo než doběhne první minuta) se do statusu nepřidá nic
    — UI tak pozná „neměří se" od „měří se a je ticho" (chybějící klíč vs.
    stav `ok`), místo aby vypnutý detektor vypadal jako zdravý feed.
    """
    if detector is None or detector.last is None:
        return {}
    verdict = detector.last
    return {
        "feed_crosscheck": verdict.state,
        "feed_crosscheck_detail": verdict.message,
        "feed_crosscheck_ibkr_dead_share": round(verdict.tally.ibkr_dead_share, 3),
        "feed_crosscheck_contracts": verdict.tally.contracts,
        # Rozlišovač „hýbe se trh?" (#764) — kontrola kalibrace prahu naživo
        "feed_crosscheck_ibkr_changed_share": round(verdict.tally.ibkr_changed_share, 3),
    }


def _setup_farm_probe(
    ib: IB,
    manager: ConnectionManager,
    settings: Settings,
    publisher: PublisherLike,
    pipelines: dict[str, InstrumentPipeline],
) -> Callable[[str], None] | None:
    """Aktivní IBKR sonda (#517 fáze B) — vrátí spouštěč, nebo None (vypnuto).

    Spouštěč se volá z alert cesty fáze A na verdikt `ibkr_suspect`; sonda pak
    mimo tick monitoru rozliší výpadek farmy od mrtvých subskripcí (a ty
    rovnou cíleně obnoví přes `manager.resubscribe_now`). Výsledek jde do
    zvonečku jako `feed_probe`.
    """
    if not settings.probe_enabled:
        return None

    async def snapshot_probe() -> bool:
        """Snapshot referenčního front future — test DORUČENÍ dat.

        Kontrakt se bere z běžící pipeline (verdikt `ibkr_suspect` předpokládá
        ≥ min_contracts sledovaných kontraktů, takže pipeline existuje).
        Živost = přišla použitelná hodnota, ne „request nespadl" — lekce
        z fáze A: zmrzlou kotaci TWS ochotně vrací pořád dokola.
        """
        pipeline = next(iter(pipelines.values()), None)
        contract = None if pipeline is None else getattr(pipeline.ticker, "contract", None)
        if contract is None:
            raise RuntimeError("žádná běžící pipeline s kontraktem podkladu")
        tickers = await ib.reqTickersAsync(contract)
        ticker = tickers[0] if tickers else None
        if ticker is None:
            return False
        values = (ticker.bid, ticker.ask, ticker.last, ticker.close)
        # `v == v` vyřazuje NaN (ib_async jím značí chybějící pole), `> 0`
        # sentinel -1, kterým IBKR hlásí prázdnou stranu knihy
        return any(v is not None and v == v and v > 0 for v in values)

    probe = FarmProbe(
        snapshot_probe,
        manager.resubscribe_now,
        # Sonda nesmí být poslední kapkou přes strop lines (ADR-0001)
        lines_free=lambda: max(0, settings.market_data_lines - count_ib_lines(ib)),
    )

    async def probe_and_report(reason: str) -> None:
        report = await probe.trigger(reason)
        if report is None:
            return
        await publisher.publish(
            "alerts",
            {
                "kind": "feed_probe",
                "symbol": "*",
                "message": report.message,
                "ts": dt.datetime.now(dt.UTC).timestamp(),
            },
        )

    # RUF006: reference na běžící sondu se drží do dokončení (#499)
    pending: set[asyncio.Task[None]] = set()

    def spawn(reason: str) -> None:
        task = asyncio.create_task(probe_and_report(reason))
        pending.add(task)
        task.add_done_callback(pending.discard)

    return spawn


def _spot_source_status(running: Sequence[InstrumentPipeline]) -> dict[str, object]:
    """Zdroj spotu do /status (#614 fáze 2a).

    Agreguje se pesimisticky: stačí JEDEN instrument na fallbacku a status
    hlásí `tasty`. Výpadek market data je vlastnost účtu, takže „půlka
    instrumentů z IBKR" je stav, o kterém se uživatel má dozvědět celý,
    ne průměrovaný do zdravě vypadající většiny.
    """
    sources = {pipeline.spot_source() for pipeline in running}
    if not sources:
        return {}
    return {"spot_source": "tasty" if "tasty" in sources else sorted(sources)[0]}


def _chain_source_status(fallback: "ChainFallback | None") -> dict[str, object]:
    """Zdroj opčního řetězu do /status (#614 fáze 2b).

    Bez zapnutého fallbacku se klíč nepřidá vůbec — stejná logika jako
    u křížové kontroly: chybějící klíč znamená „tahle ochrana neběží",
    ne „běží a je vše v pořádku". Tiché přepnutí zdroje zakazuje ADR-0025
    pravidlo 5, takže stav musí být čitelný i bez alertu.
    """
    if fallback is None:
        return {}
    return {"chain_source": fallback.active_source}


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
    gamma_cliff_repository: GammaCliffRepository | None = None,
    vol_regime_repository: VolRegimeRepository | None = None,
    db: Engine | None = None,
    pacing_guard: PacingGuard | None = None,
    fa_repository: FaValidationRepository | None = None,
    alpha_repository: FaAlphaRepository | None = None,
    news_ticks: NewsTickCollector | None = None,
    provider: MarketDataProviderLike | None = None,
    line_gauge: LineGauge | None = None,
    oi_fallback: Callable[[OptionContractSpec], float | None] | None = None,
    spot_fallback_source: Callable[[str], tuple[float | None, bool]] | None = None,
    chain_fallback_source: (
        Callable[[Sequence[OptionContractSpec]], dict[OptionContractSpec, CachedQuote] | None]
        | None
    ) = None,
    futures_cvd: FuturesCvdTracker | None = None,
) -> InstrumentPipeline:
    """Produkční sestavení pipeline jednoho podkladu nad ib_async."""
    # Provider (#613): svazek datových zdrojů; default = IBKR (jediný zapojený
    # do výpočtů, dokud shadow fáze M7 neskončí)
    if provider is None:
        provider = IbkrProvider(ib, line_gauge)
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
    # Fallback na tasty (#614) — jen když je zapnutý A je z čeho brát
    tasty_spot = spot_fallback_source
    spot_fallback = (
        SpotFallback(
            stale_after_s=settings.tasty_spot_stale_after_s,
            recover_after_s=settings.tasty_spot_recover_after_s,
        )
        if settings.tasty_spot_fallback and tasty_spot is not None
        else None
    )

    def _publish_spot(price: float, source: str) -> None:
        loop.create_task(
            publisher.publish(
                f"spot.{symbol}",
                {
                    "ts": dt.datetime.now(dt.UTC).isoformat(),
                    "price": price,
                    # Zdroj jde s každým tickem (#614): tichý fallback je
                    # zakázaný, uživatel musí poznat, odkud se dívá
                    "source": source,
                },
            )
        )

    def on_spot_tick(ticker: Ticker) -> None:
        if stopped:
            return
        price = ticker.last if ticker.last == ticker.last else ticker.marketPrice()
        if spot_fallback is not None:
            decision = spot_fallback.on_ibkr(price, loop.time())
            if decision.switched:
                logger.info("Spot %s: zpět na IBKR (feed se zotavil, #614)", symbol)
            if decision.price is None:
                return  # během fallbacku se IBKR ticky nepublikují
            price = decision.price
        published = spot_streamer.sample(price, loop.time())
        if published is None:
            return
        _publish_spot(published, spot_fallback.active_source if spot_fallback else "ibkr")

    async def spot_fallback_loop() -> None:
        """Hlídá mlčící IBKR i mimo jeho ticky (#614).

        Bez vlastní smyčky by se výpadek nepoznal: `on_spot_tick` se při
        mlčícím feedu prostě nevolá, takže rozhodnutí musí přijít odjinud.
        """
        while not stopped and spot_fallback is not None and tasty_spot is not None:
            await asyncio.sleep(SPOT_FALLBACK_POLL_S)
            try:
                price, fresh = tasty_spot(symbol)
                decision = spot_fallback.resolve(loop.time(), tasty_price=price, tasty_fresh=fresh)
                if decision.switched:
                    logger.warning(
                        "Spot %s: IBKR mlčí, přebírá tastytrade (#614) — "
                        "typicky souběh s mobilem (error 10197)",
                        symbol,
                    )
                    await publisher.publish(
                        "alerts",
                        {
                            "kind": "spot_fallback",
                            "symbol": symbol,
                            "message": (
                                f"{symbol}: IBKR přestal posílat cenu, spot přebírá "
                                f"tastytrade. Data běží dál, jen z jiného zdroje."
                            ),
                            "ts": dt.datetime.now(dt.UTC).timestamp(),
                        },
                    )
                if decision.price is not None:
                    published = spot_streamer.sample(decision.price, loop.time())
                    if published is not None:
                        _publish_spot(published, decision.source)
            except Exception:
                logger.exception("Spot fallback selhal — příští cyklus jede dál")

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

    streamer = provider.quote_streamer()
    # Následující expirace (čtení positioningu příští seance): sekundární runtime
    # sweepuje v nižší kadenci, píše jen snapshots + levels své expirace
    next_runtime: EngineRuntime | None = None
    next_info: ExpiryInfo | None = None
    next_band: StrikeBand | None = None
    if settings.sweep_next_expiry and len(infos) > 1:
        next_info = infos[1]
        # Pásmo se dál roztahuje v run_cycle (#442) — bez toho zamrzlo na startu
        next_band = discovery.initial_band(next_info, spot)
        next_contracts = build_contracts(underlying, next_info, next_band)
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
            oi_fallback=oi_fallback,
            chain_fallback=chain_fallback_source,
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
        futures_cvd=futures_cvd,  # CVD podkladu (#829) — jen primární runtime
        push_status=False,  # agregovaný status pushuje orchestrátor
        oi_fallback=oi_fallback,
        chain_fallback=chain_fallback_source,
    )

    # Historical backfill 1min barů (SPEC 3.6, #221): aktuální den + retention
    # okno při startu, jednodenní re-backfill po výpadku real-time streamu
    backfiller = UnderlyingBackfiller(provider.historical(front), pacing_guard, settings)

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
    # Hlídač mlčícího IBKR spotu (#614); bez fallbacku se úloha nezakládá
    fallback_task = asyncio.create_task(spot_fallback_loop()) if spot_fallback is not None else None

    def on_stop() -> None:
        nonlocal stopped
        stopped = True
        backfill_task.cancel()
        if fallback_task is not None:
            fallback_task.cancel()
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
        archiver=OIArchiver(oi_repository, provider.oi_fetcher(), settings),
        oi_repository=oi_repository,
        ticker=fut_ticker,
        minute_bars=minute_bars,
        forming_bar=lambda: aggregator.current,
        on_stop=on_stop,
        spot=spot,
        spot_source=(lambda: spot_fallback.active_source if spot_fallback else "ibkr"),
        archive_contracts=archive_contracts,
        next_runtime=next_runtime,
        next_info=next_info,
        next_band=next_band,
        backfill_today=backfill_today,
        fa_repository=fa_repository,
        alpha_repository=alpha_repository,
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
                feature_writer=writer if settings.feature_log_enabled else None,
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
        gamma_cliff=(
            GammaCliffCollector(
                symbol=symbol,
                repository=gamma_cliff_repository,
                db=db,
                data_dir=settings.data_dir,
            )
            if gamma_cliff_repository is not None and db is not None
            else None
        ),
        vol_regime=(
            VolRegimeCollector(
                symbol=symbol,
                repository=vol_regime_repository,
                data_dir=settings.data_dir,
            )
            if vol_regime_repository is not None
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


async def wait_for_connection(manager: ConnectionManager, timeout_s: float) -> bool:
    """Počká na IBKR nejvýš `timeout_s`; vrací, jestli se povedlo (#756).

    Návrat `False` NENÍ chyba — supervisor `ConnectionManager` se pokouší
    připojovat dál a hlavní smyčka pipelines založí, jakmile spojení bude.
    Smysl časového stropu je jen ten, aby se za čekáním nezasekly části
    enginu, které na IBKR vůbec nestojí.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while manager.state is not ConnectionState.CONNECTED:
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.5)
    return True


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # Hned po logování, ať je dump k dispozici i pro pád při startu (#771)
    install_stack_dump()
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from exc

    # Dev laboratoř jen s tastytrade (#623): bez IBKR, bez výpočtů, bez zápisů —
    # celý zbytek main() (TWS connect, pipelines) se přeskočí
    if settings.tasty_only:
        await run_tasty_only(settings)
        return

    api_base = os.environ.get("GEXLENS_API_BASE", "http://127.0.0.1:8000")
    api_token = os.environ.get("GEXLENS_API_TOKEN", "").strip()
    if not api_token:
        # Bez tokenu API interní ingest odmítá (#542) — stav i kanály by tiše
        # přestaly chodit, tak ať je důvod vidět hned při startu
        logger.error(
            "GEXLENS_API_TOKEN není nastaven — API odmítne /internal/*, "
            "UI zůstane bez živých dat (vygeneruj přes scripts/init-secrets.ps1)"
        )
    publisher = HttpPublisher(api_base, api_token)
    ib = IB()
    # Obsazené market data lines (#630): měřené z registru subskripcí,
    # jediný gauge nad sdíleným spojením — strop účtu platí napříč pipeline
    line_gauge = LineGauge(lambda: count_ib_lines(ib))
    provider = IbkrProvider(ib, line_gauge)
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
    # Dlouhý výpadek spojení hlásí zvoneček (#770) — registrace PŘED start(),
    # ať dozor platí od první vteřiny (i pro „TWS po startu stroje neběží")
    _watch_connection_stall(manager, settings, publisher)
    await manager.start()
    # Čekání na IBKR je OHRANIČENÉ (#756). Dokud tu byla nekonečná smyčka,
    # zůstal za ní celý zbytek main() — schéma DB, pipelines, tastytrade větev
    # i spot fallback z #614. Bez běžícího TWS tak neběželo NIC: engine se
    # dvacet minut po startu Windows dokola pokoušel připojit a v logu nebyl
    # jediný řádek `tasty`. Fallback z #614 přitom má chránit právě proti
    # mlčícímu IBKR — jen doteď uměl jen ten případ, kdy spojení nejdřív bylo.
    if not await wait_for_connection(manager, settings.startup_connect_wait_s):
        logger.warning(
            "IBKR se za %.0f s nepřipojil — engine startuje bez něj. Opční řetěz "
            "a výpočty naskočí samy, jakmile spojení bude; do té doby jede jen "
            "tastytrade větev (cena podkladu). Typicky: nespuštěná TWS po startu stroje.",
            settings.startup_connect_wait_s,
        )

    writer = SnapshotWriter(settings)
    db = create_engine(settings.database_url)
    oi_repository = OIEodRepository(db)
    await asyncio.to_thread(oi_repository.ensure_schema)
    # Denní FA validace (#232): body open-ratio se sbírají samy po OI archivu
    fa_repository = FaValidationRepository(db)
    await asyncio.to_thread(fa_repository.ensure_schema)
    # Ranní kalibrace α (#232 fáze 2): netflow vs. skutečné ΔOI z archivu
    alpha_repository = FaAlphaRepository(db)
    await asyncio.to_thread(alpha_repository.ensure_schema)
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
        # Kandidáti uložení starou konvencí UTC půlnoci se při startu dorovnají
        # na settle konvenci z věčného archivu barů (#498)
        await asyncio.to_thread(
            recompute_stale_candidates,
            t6_repository,
            settings.data_dir,
            settings.t6_trigger_pct,
        )
    gamma_cliff_repository: GammaCliffRepository | None = None
    if settings.gamma_cliff_enabled:
        gamma_cliff_repository = GammaCliffRepository(db)
        await asyncio.to_thread(gamma_cliff_repository.ensure_schema)

    # Volatilitní režim (ADR-0028): čte jen bary, žádná IBKR linka navíc
    vol_regime_repository = VolRegimeRepository(db)
    await asyncio.to_thread(vol_regime_repository.ensure_schema)

    # Broker headlines z ticku 292 (#291): schéma SentimentLensu sdílí obě
    # služby, engine do něj jen zapisuje
    news_ticks: NewsTickCollector | None = None
    article_catch_up: asyncio.Task[int] | None = None
    if settings.ibkr_news_enabled:
        await asyncio.to_thread(ensure_sentiment_schema, db)
        news_ticks = NewsTickCollector(db)
        # Plné znění článků (#743): živé headlines hned po pushi, historie
        # (vč. eventů z doby před #743 a z výpadků) catch-upem po KAŽDÉM
        # (re)connectu — jednorázová kontrola při startu by při startu bez
        # TWS (#756) nechala backlog neplněný do konce běhu procesu
        article_fetcher = ArticleFetcher(ib, db) if settings.ibkr_news_articles_enabled else None
        await _start_broker_news(ib, manager, news_ticks, publisher, article_fetcher)
        if article_fetcher is not None:
            fetcher = article_fetcher

            def _report_catch_up(task: asyncio.Task[int]) -> None:
                if not task.cancelled() and task.exception() is not None:
                    logger.error("Catch-up článků selhal: %r", task.exception())

            async def _article_catch_up() -> None:
                nonlocal article_catch_up
                if article_catch_up is not None and not article_catch_up.done():
                    return  # předchozí catch-up ještě běží — druhý nefrontovat
                # Reference se drží (RUF006); výjimku hlásí callback
                article_catch_up = asyncio.create_task(fetcher.catch_up())
                article_catch_up.add_done_callback(_report_catch_up)

            manager.on_resubscribe(_article_catch_up)
            if manager.state is ConnectionState.CONNECTED:
                await _article_catch_up()

    retention = RetentionJob(settings)
    last_purge_date: dt.date | None = None
    # Dohled nad volným místem (#773): měří datový disk (bind mount = čísla
    # hostitele) a velikost PostgreSQL; alerty do zvonečku, úklid řeší #757
    disk_watch = DiskWatch(
        settings.data_dir,
        db,
        warn_free_gb=settings.disk_free_warn_gb,
        crit_free_gb=settings.disk_free_crit_gb,
        db_alert_gb=settings.db_size_alert_gb,
    )
    # Globální rate limiter historical requestů (SPEC 3.6) — sdílený všemi pipeline
    pacing_guard = PacingGuard()

    pipelines: dict[str, InstrumentPipeline] = {}

    # ── tastytrade shadow (#613) — měří do feed_comparison; k tomu OI fill
    # (#664, předsunutý kus #614): chybějící archivní OI doplní Summary ──
    shadow_stop = asyncio.Event()
    shadow_tasks: list[asyncio.Task[None]] = []
    shadow_chain: dict[str, ChainSymbols] = {}
    # Streamer symbol front futures per instrument (#614) — zdroj spotu, když
    # IBKR přestane posílat (mobil přetáhl market data, výpadek farmy)
    shadow_front_future: dict[str, str] = {}
    # CVD podkladu (#829): jedna instance pro celý engine, runtimes z ní čtou
    # minutu. Bez tasty větve zůstane bez registrací → řada je prostě NULL.
    futures_cvd = FuturesCvdTracker()
    # Extended expirace z tasty (#616 4a): plán per symbol, plní ho denní
    # obnova chain map; snapshot smyčka z něj čte
    extended_plan: dict[str, list[str]] = {}
    tasty_spot_lookup: Callable[[str], tuple[float | None, bool]] | None = None
    tasty_oi_lookup: Callable[[OptionContractSpec], float | None] | None = None
    # Fallback celého řetězu (#614 fáze 2b): jedna instance pro celý engine —
    # market data lines jsou vlastnost účtu, takže výpadek bere ES i NQ naráz
    chain_fallback: ChainFallback | None = None
    chain_quotes_lookup: (
        Callable[[Sequence[OptionContractSpec]], dict[OptionContractSpec, CachedQuote] | None]
        | None
    ) = None
    chain_verdict_hook: Callable[[CrossCheckVerdict], Awaitable[None]] | None = None
    # Stav tastytrade větve pro /status a Settings (#706) — None = větev neběží;
    # nepřítomnost polí je pro UI jiný stav než „běží a je odpojená"
    tasty_status_fields: Callable[[], dict[str, object]] | None = None
    # Křížová kontrola (#517 A) — None, dokud neběží shadow větev; status ji
    # čte z hlavní smyčky, proto musí být viditelná i při vypnutém shadow
    crosscheck: CrossCheckDetector | None = None
    publish_crosscheck: Callable[[CrossCheckVerdict], Awaitable[None]] | None = None
    if settings.tasty_enabled and settings.tasty_client_secret and settings.tasty_refresh_token:
        tasty_session = TastySession(
            TastyCredentials(
                client_secret=settings.tasty_client_secret,
                refresh_token=settings.tasty_refresh_token,
            )
        )
        symbol_map = SymbolMap(tasty_session)
        tasty_cache = TastyChainCache()
        # Recorder surových opčních printů (#795): učicí data, která jinak
        # nenávratně mizí. Fan-out callbacku — cache i recorder vidí tytéž eventy.
        trades_recorder = TradesRecorder() if settings.tasty_trades_record else None

        def _tasty_event(event_type: str, values: list[object]) -> None:
            tasty_cache.on_event(event_type, values)
            if trades_recorder is not None:
                trades_recorder.on_event(event_type, values)
            # CVD podkladu (#829): tracker si sám vybere jen printy
            # registrovaných front futures, opční projdou bez práce
            futures_cvd.on_event(event_type, values)

        tasty_stream = DxLinkStream(tasty_session.quote_token, _tasty_event)

        def _tasty_status() -> dict[str, object]:
            """Stav větve do /status (#706): spojení, subskripce, pokrytí, čerstvost."""
            counts = tasty_cache.field_counts()
            fields: dict[str, object] = {
                "tasty_connected": tasty_stream.connected,
                "tasty_reconnects": tasty_stream.reconnects,
                "tasty_symbols": tasty_cache.symbols_tracked(),
                "tasty_quotes": counts["quotes"],
                "tasty_greeks": counts["greeks"],
                "tasty_oi": counts["summary"],
                "tasty_trades": counts["trades"],
            }
            if tasty_cache.last_event_at is not None:
                fields["tasty_last_event_ts"] = tasty_cache.last_event_at.isoformat()
            if trades_recorder is not None:
                fields["tasty_trades_recorded"] = trades_recorder.recorded
            if greeks_validator is not None:
                fields.update(greeks_validator.status_fields())
            if extended_plan:
                # Zdroj per expirace (#616, DoD 3): UI musí poznat tasty expirace
                fields["tasty_extended_expiries"] = {
                    symbol: list(planned) for symbol, planned in sorted(extended_plan.items())
                }
            return fields

        tasty_status_fields = _tasty_status
        # Zapisovatel porovnání je VOLITELNÝ odběratel monitoru (#763): bez něj
        # se řádky vůbec nestaví a `feed_comparison` přestane růst, ale tally
        # pro detektor i oba fallbacky běží dál.
        comparison_repository: FeedComparisonRepository | None = None
        if settings.tasty_comparison_write:
            comparison_repository = FeedComparisonRepository(db)
            await asyncio.to_thread(comparison_repository.ensure_schema)

        def shadow_contracts() -> dict[OptionContractSpec, CachedQuote]:
            merged: dict[OptionContractSpec, CachedQuote] = {}
            for pipeline in pipelines.values():
                merged.update(pipeline.runtime.scheduler.quotes())
                if pipeline.next_runtime is not None:
                    merged.update(pipeline.next_runtime.scheduler.quotes())
            return merged

        def shadow_oi_snapshot() -> dict[tuple[str, str, float, str], float]:
            """Denní archiv IBKR pro OI porovnání (#664) — klíč (symbol, expiry, strike, right)."""
            today = dt.datetime.now(dt.UTC).date()
            merged_oi: dict[tuple[str, str, float, str], float] = {}
            for pipeline_symbol in list(pipelines):
                snapshot = oi_repository.snapshot(pipeline_symbol, today)
                for (expiry, strike, right), oi_value in snapshot.items():
                    merged_oi[(pipeline_symbol, expiry, strike, right)] = oi_value
            return merged_oi

        # Křížová kontrola IBKR × tasty (#517 fáze A): tasty čerstvé při mrtvém
        # IBKR vylučuje „tichý trh" — jediná nejednoznačnost, kterou pasivní
        # vrstva sama neuměla rozhodnout. Nula requestů, nula linek.
        if settings.crosscheck_enabled:
            crosscheck = CrossCheckDetector(
                share_threshold=settings.crosscheck_share_threshold,
                minutes_threshold=settings.crosscheck_minutes,
                cooldown_minutes=settings.crosscheck_cooldown_minutes,
                change_threshold=settings.crosscheck_change_threshold,
            )
            probe_spawner = _setup_farm_probe(ib, manager, settings, publisher, pipelines)

            async def _publish_crosscheck(verdict: CrossCheckVerdict) -> None:
                # Mrtvá záloha (#764) má vlastní kanál: nejde o „data jsou
                # špatná" (feed_crosscheck), ale o „záloha není k dispozici"
                await publisher.publish(
                    "alerts",
                    {
                        "kind": "feed_backup_dead" if verdict.backup_dead else "feed_crosscheck",
                        "symbol": "*",
                        "message": verdict.message,
                        "ts": dt.datetime.now(dt.UTC).timestamp(),
                    },
                )
                # Aktivní sonda (#517 fáze B): `ibkr_suspect` říká, že problém
                # je na straně IBKR — sonda rozliší farmu od mrtvých subskripcí
                # a subskripce rovnou cíleně obnoví. Běží mimo tick monitoru,
                # aby snapshot (až 10 s) nezdržel minutové porovnání.
                if probe_spawner is not None and verdict.state == "ibkr_suspect":
                    probe_spawner(verdict.message)

            publish_crosscheck = _publish_crosscheck

        if settings.tasty_chain_fallback and crosscheck is not None:
            chain_fallback = ChainFallback(recover_minutes=settings.tasty_chain_recover_minutes)

            def _chain_quotes(
                specs: Sequence[OptionContractSpec],
            ) -> dict[OptionContractSpec, CachedQuote] | None:
                """Řetěz z tasty, nebo None = ať si runtime vezme IBKR sweep.

                Volá se na hranici snímku (ADR-0025 pravidlo 3); samotné
                rozhodnutí padlo minutu předtím ve `_chain_verdict`, tady se
                už jen skládají hodnoty.
                """
                if chain_fallback is None or chain_fallback.active_source != "tasty":
                    return None
                if not specs:
                    return None
                # Všechny specs jedné pipeline sdílejí symbol; mapa je per produkt
                chain = shadow_chain.get(specs[0].symbol)
                return tasty_chain_quotes(
                    specs,
                    chain,
                    tasty_cache,
                    now_utc_ts=dt.datetime.now(dt.UTC).timestamp(),
                    now_monotonic=time.monotonic(),
                    max_age_ms=int(settings.tasty_chain_max_age_s * 1000),
                )

            chain_quotes_lookup = _chain_quotes

            async def _chain_verdict(verdict: CrossCheckVerdict) -> None:
                """Minutový verdikt křížové kontroly → zdroj řetězu pro další snímek."""
                if chain_fallback is None:
                    return
                decision = chain_fallback.observe(verdict)
                if not decision.switched:
                    return
                logger.warning("Fallback řetězu (#614): %s", decision.message)
                await publisher.publish(
                    "alerts",
                    {
                        "kind": "chain_fallback",
                        "symbol": "*",
                        "message": decision.message
                        + (
                            ". Řady CumΔ a net objem po dobu fallbacku stojí — "
                            "tastytrade denní objem v sémantice IBKR nedodává."
                            if decision.source == "tasty"
                            else "."
                        ),
                        "ts": dt.datetime.now(dt.UTC).timestamp(),
                    },
                )

            chain_verdict_hook = _chain_verdict
            logger.info(
                "Fallback řetězu z tasty ZAPNUT (#614 fáze 2b) — spouští ho verdikt "
                "křížové kontroly, návrat po %d čistých minutách",
                settings.tasty_chain_recover_minutes,
            )

        # Greeks validátor (#614 finále): měřené prahy, jen hlásí (22. 8.)
        greeks_validator = (
            GreeksValidator(
                share_threshold=settings.greeks_suspect_share,
                minutes_threshold=settings.greeks_suspect_minutes,
            )
            if settings.greeks_validator_enabled
            else None
        )

        async def _publish_greeks_alert(alert: GreeksAlert) -> None:
            await publisher.publish(
                "alerts",
                {
                    "kind": "greeks_suspect",
                    "symbol": alert.symbol,
                    "message": alert.message,
                    "ts": dt.datetime.now(dt.UTC).timestamp(),
                },
            )

        monitor = FeedMonitor(
            comparison_repository,
            tasty_cache,
            shadow_contracts,
            lambda: dict(shadow_chain),
            oi_source=shadow_oi_snapshot,
            detector=crosscheck,
            on_alert=publish_crosscheck,
            on_verdict=chain_verdict_hook,
            greeks_validator=greeks_validator,
            on_greeks_alert=_publish_greeks_alert,
        )

        def _tasty_spot(symbol: str) -> tuple[float | None, bool]:
            """(cena podkladu z tasty, je čerstvá?) — vstup fallbacku (#614).

            Cena je mid z bid/ask front futures. `last` se schválně nebere:
            u futures může být poslední obchod starý minuty, kdežto kotace
            se drží živá, a fallback má nahradit ŽIVÝ spot.
            """
            streamer = shadow_front_future.get(symbol)
            if streamer is None:
                return None, False
            state = tasty_cache.state(streamer)
            if state is None or state.quote.updated_at is None:
                return None, False
            bid, ask = state.quote.bid, state.quote.ask
            if bid is None or ask is None or bid <= 0 or ask <= 0:
                return None, False
            age_s = (dt.datetime.now(dt.UTC) - state.quote.updated_at).total_seconds()
            return (bid + ask) / 2.0, age_s <= settings.tasty_spot_max_age_s

        tasty_spot_lookup = _tasty_spot

        if settings.tasty_oi_fill:

            def _tasty_oi(spec: OptionContractSpec) -> float | None:
                chain = shadow_chain.get(spec.symbol)
                if chain is None:
                    return None
                streamer = chain.streamer_symbol(spec)
                if streamer is None:
                    return None
                state = tasty_cache.state(streamer)
                if state is None:
                    return None
                # Summary je denní agregát — stáří se nehlídá: event chodí při
                # subskripci a při změně, OI se přes den nemění (ADR-0027)
                return state.summary.open_interest

            tasty_oi_lookup = _tasty_oi
            logger.info(
                "tasty OI fill ZAPNUT — díry denního archivu doplní Summary (#664, kus #614)"
            )

        def shadow_target_symbols() -> list[str]:
            """Instrumenty, pro které se drží tasty subskripce.

            Nejen běžící pipelines (#756): bez připojeného IBKR neběží žádná,
            takže by se řetěz ani front future nikdy neodebraly a fallback by
            při startu bez TWS neměl z čeho stavět. Konfigurovaný seznam je
            dostupný vždy; symboly přidané do watchlistu se přidají, jakmile
            jejich pipeline naskočí.
            """
            return sorted({*settings.symbol_list, *pipelines})

        def ibkr_expiries_of(symbol: str) -> set[str]:
            """Expirace držené IBKR pipeline (aktivní + next) — vlastnická množina."""
            pipeline = pipelines.get(symbol)
            if pipeline is None:
                return set()
            held = {pipeline.runtime.expiry}
            if pipeline.next_runtime is not None:
                held.add(pipeline.next_runtime.expiry)
            return held

        async def shadow_symbols_loop() -> None:
            """Denní obnova chain mapy + průběžné dorovnání subskripce."""
            while not shadow_stop.is_set():
                try:
                    today = dt.datetime.now(dt.UTC).date()
                    # Jedna mapa per produkt; ES i NQ jsou v témže chain endpointu
                    # svých produktů — mapy se drží per symbol pipeline
                    symbols: set[str] = set()
                    for symbol in shadow_target_symbols():
                        chain = await symbol_map.chain(symbol, today)
                        shadow_chain[symbol] = chain
                        symbols |= tracked_symbols(list(shadow_contracts().keys()), chain)
                        # Extended expirace (#616 4a): šířka mimo IBKR množinu —
                        # disjunktnost hlídá validate_disjoint (překryv = chyba)
                        if settings.tasty_extended_enabled:
                            planned = plan_extended_expiries(
                                chain,
                                ibkr_expiries_of(symbol),
                                today=today,
                                horizon_days=settings.tasty_extended_horizon_days,
                            )
                            validate_disjoint(planned, ibkr_expiries_of(symbol))
                            extended_plan[symbol] = planned
                            # Pásmo kolem spotu — bez něj ES (49 expirací × plná
                            # šířka) přeteče kapacitu subskripce a server tiše
                            # nedodá NIC (nedělní noc 23. 8.: NQ psalo, ES mlčelo)
                            spot_price, spot_fresh = _tasty_spot(symbol)
                            symbols |= extended_streamers(
                                chain,
                                planned,
                                center=spot_price if spot_fresh else None,
                                band_pct=settings.tasty_extended_band_pct,
                            )
                        # Podklad (#614): bez něj by při výpadku IBKR zamrzl
                        # cenový graf, i kdyby řetěz z tasty tekl dál
                        front = await symbol_map.front_future(symbol)
                        if front:
                            shadow_front_future[symbol] = front
                            symbols.add(front)
                            # CVD podkladu (#829) — registrace zároveň ošetří
                            # roll kontraktu (starý streamer se odpojí)
                            futures_cvd.register(symbol, front)
                    if trades_recorder is not None:
                        # Jen chain symboly — podklad záměrně ne (viz recorder):
                        # jeho printy jsou miliony/den a CumΔ podkladu nese IBKR
                        trades_recorder.set_mapping(
                            {
                                streamer: sym
                                for sym, chain in shadow_chain.items()
                                for streamer in chain.by_contract.values()
                            }
                        )
                    if symbols:
                        await tasty_stream.set_symbols(symbols)
                except Exception:
                    logger.exception("Shadow symbols refresh selhal — zkusí se za minutu")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(shadow_stop.wait(), timeout=60.0)

        async def orphan_spot_loop() -> None:
            """Cena z tasty pro instrumenty BEZ běžící pipeline (#756).

            Spot fallback z #614 žije uvnitř pipeline, takže dokud IBKR
            nepřipojí, nemá kdo cenu publikovat — a uživatel vidí prázdný graf,
            i když tasty data teče. Tahle smyčka díru zaplní a jakmile pipeline
            naskočí, sama pro daný symbol umlkne: dva zdroje na jeden kanál by
            v grafu vypadaly jako skákající cena.
            """
            while not shadow_stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(shadow_stop.wait(), timeout=SPOT_FALLBACK_POLL_S)
                if shadow_stop.is_set():
                    return
                try:
                    for symbol in shadow_target_symbols():
                        if symbol in pipelines:
                            continue  # publikuje pipeline, sem nepatří
                        price, fresh = _tasty_spot(symbol)
                        if price is None or not fresh:
                            continue
                        await publisher.publish(
                            f"spot.{symbol}",
                            {
                                "ts": dt.datetime.now(dt.UTC).isoformat(),
                                "price": price,
                                "source": "tasty",
                            },
                        )
                except Exception:
                    logger.exception("Spot bez pipeline selhal — příští cyklus jede dál")

        async def extended_snapshot_loop() -> None:
            """Minutová konsolidace extended expirací (#616 4a).

            Řádky vznikají jen z čerstvých tasty kotací — zavřený trh přirozeně
            nezapisuje nic. Spot bere z tasty front future (nezávislé na IBKR,
            takže extended šířka žije i při výpadku — duch ADR-0025 dodatku).
            """
            last_idle_log = 0.0
            while not shadow_stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(shadow_stop.wait(), timeout=60.0)
                if shadow_stop.is_set():
                    return
                try:
                    now_utc = dt.datetime.now(dt.UTC)
                    ts_min = now_utc.replace(second=0, microsecond=0)
                    minute_of_day = ts_min.hour * 60 + ts_min.minute
                    written = 0
                    gates: dict[str, str] = {}
                    for symbol, planned in list(extended_plan.items()):
                        chain = shadow_chain.get(symbol)
                        if chain is None or not planned:
                            gates[symbol] = "bez plánu/chainu"
                            continue
                        spot, fresh = _tasty_spot(symbol)
                        if spot is None or not fresh:
                            gates[symbol] = f"spot={spot!r} fresh={fresh}"
                            continue
                        for expiry in planned:
                            if not cadence_due(
                                expiry,
                                today=now_utc.date(),
                                minute_of_day=minute_of_day,
                                near_days=settings.tasty_extended_near_days,
                                far_interval_min=settings.tasty_extended_far_interval_min,
                            ):
                                continue
                            rows, oi_missing = build_snapshot_rows(
                                chain,
                                expiry,
                                tasty_cache,
                                ts_min=ts_min,
                                spot=spot,
                                now_utc=now_utc,
                                max_age_s=float(MAX_AGE_MS) / 1000.0,
                            )
                            if not rows:
                                continue
                            day = ts_min.date()
                            await asyncio.to_thread(writer.write_minute, symbol, expiry, day, rows)
                            written += len(rows)
                            if oi_missing:
                                await asyncio.to_thread(
                                    writer.write_oi_missing, symbol, expiry, day, oi_missing
                                )
                    # Ticho ≠ úspěch (#616 4a smoke): první noc se nezapsalo nic
                    # a nebylo z čeho poznat proč — brány se proto hlásí. Zapsané
                    # minuty se nelogují (60×/h by byl spam), jen přechody a ticho.
                    if written == 0 and time.monotonic() - last_idle_log > 600:
                        last_idle_log = time.monotonic()
                        logger.info(
                            "Extended: tuto minutu 0 řádků — brány: %s (plán %s)",
                            gates or "prošly, ale build vrátil prázdno",
                            {s: len(p) for s, p in extended_plan.items()},
                        )
                    elif written and last_idle_log:
                        last_idle_log = 0.0
                        logger.info("Extended: zápis obnoven (%d řádků tuto minutu)", written)
                except Exception:
                    logger.exception("Extended snapshoty selhaly — příští minuta jede dál")

        async def trades_flush_loop() -> None:
            """Minutový flush recorderu (#795) do trades/{sym}/{den}.parquet.

            Poslední drain proběhne i po signálu stop — rozdělaná minuta by se
            jinak ztratila. Parquet zápis je blokující, proto to_thread.
            """
            assert trades_recorder is not None
            while True:
                stopped = shadow_stop.is_set()
                if not stopped:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(shadow_stop.wait(), timeout=60.0)
                        stopped = True
                try:
                    for (root, day), rows in trades_recorder.drain().items():
                        await asyncio.to_thread(writer.write_tasty_trades, root, day, rows)
                except Exception:
                    logger.exception("Flush opčních tradů selhal — příští minuta jede dál")
                if stopped:
                    return

        shadow_tasks = [
            asyncio.create_task(tasty_stream.run(shadow_stop)),
            asyncio.create_task(monitor.run(shadow_stop)),
            asyncio.create_task(shadow_symbols_loop()),
            asyncio.create_task(orphan_spot_loop()),
        ]
        if trades_recorder is not None:
            shadow_tasks.append(asyncio.create_task(trades_flush_loop()))
            logger.info("Záznam opčních TimeAndSale printů ZAPNUT (#795) → data/trades/")
        if settings.tasty_extended_enabled:
            shadow_tasks.append(asyncio.create_task(extended_snapshot_loop()))
            logger.info(
                "Extended expirace ZAPNUTY (#616 4a): horizont %d dnů, kadence 1 min/≤%d dnů "
                "jinak %d min — snapshoty s BS greeks z tasty kotací",
                settings.tasty_extended_horizon_days,
                settings.tasty_extended_near_days,
                settings.tasty_extended_far_interval_min,
            )
        # Reference na tasks se drží (RUF006) — GC by je jinak uklidil před doběhem
        logger.info(
            "tastytrade větev ZAPNUTA (%d úloh); porovnání do feed_comparison: %s (#763)",
            len(shadow_tasks),
            "zapisuje se" if comparison_repository is not None else "VYPNUTO",
        )
    elif settings.tasty_enabled:
        logger.warning(
            "tastytrade větev je zapnutá, ale chybí tajemství v env "
            "(GEXLENS_TASTY_CLIENT_SECRET, _REFRESH_TOKEN) — nespouští se, "
            "takže při výpadku IBKR nebude fallback (#614)"
        )
    # Symboly po selhaném setupu: cooldown v cyklech do dalšího pokusu
    setup_cooldown = SetupCooldown()

    async def release_cooldown_after_reconnect() -> None:
        """Po reconnectu se setup zkusí hned (#455).

        Selhání ze staré, rozpadlé relace o novém spojení nic neříká; bez
        uvolnění by pipeline naskočila až po SETUP_RETRY_CYCLES minutách.
        """
        released = setup_cooldown.clear()
        if released:
            logger.info("Reconnect ruší cooldown setupu: %s", ", ".join(released))

    manager.on_resubscribe(release_cooldown_after_reconnect)
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
            keys = (
                [spec.key for spec in RUNTIME_SETTINGS]
                + [spec.key for spec in CONNECTION_SETTINGS]
                + ["subscription_alert_enabled", "ibkr_host"]
            )
            stored = await asyncio.to_thread(watchlist_reader.settings_map, keys)
            subscription_alerts["enabled"] = stored.get("subscription_alert_enabled") is not False
            restart_pipelines = apply_runtime_settings(settings, stored)
            # Změna spojení (#446): odpojením se supervisor ConnectionManageru
            # sám připojí znovu — už s novým hostem/portem/clientId. Pipeline
            # se musí postavit znovu, subskripce patřily starému spojení.
            if apply_connection_settings(settings, stored):
                logger.info(
                    "Změna připojení k IBKR — přepojuji na %s:%d",
                    settings.ibkr_host,
                    settings.ibkr_port,
                )
                ib.disconnect()
                restart_pipelines = True
                # Cooldown patřil starému spojení — na novém portu/hostu se
                # musí zkusit hned, jinak uživatel opraví Settings a graf
                # zůstane prázdný celý cooldown (#455)
                setup_cooldown.clear()
            if restart_pipelines:
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
                # Resubskripce nové seance vyrobí nárazově error 354 (#772:
                # 18./19. 8. skok 5→23 přesně o půlnoci UTC) — očekávaný
                # přechod, ne porucha; alertovací práh ho nemá počítat
                subscription_errors.excuse(
                    settings.subscription_error_rollover_grace_s, now=time.monotonic()
                )

        setup_cooldown.tick()
        eligible = [symbol for symbol in desired if not setup_cooldown.blocked(symbol)]

        plan = plan_instruments(pipelines.keys(), eligible, settings.max_instruments)
        for symbol in plan.stop:
            logger.info("Zastavuji pipeline %s (odebráno z watchlistu)", symbol)
            pipelines.pop(symbol).stop()
        for symbol in plan.start:
            # Bez spojení nemá zakládání smysl (#756): discovery i subskripce
            # jsou IBKR volání, takže by cyklus co minutu spálil pokus, spadl
            # na ConnectionError a zaplnil log. Existující pipeline se NEruší —
            # `plan.stop` se řídí watchlistem, ne stavem spojení, aby krátký
            # výpadek nezboural běžící instrumenty.
            if manager.state is not ConnectionState.CONNECTED:
                break
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
                    gamma_cliff_repository=gamma_cliff_repository,
                    vol_regime_repository=vol_regime_repository,
                    db=db,
                    pacing_guard=pacing_guard,
                    fa_repository=fa_repository,
                    alpha_repository=alpha_repository,
                    news_ticks=news_ticks,
                    provider=provider,
                    line_gauge=line_gauge,
                    oi_fallback=tasty_oi_lookup,
                    chain_fallback_source=chain_quotes_lookup,
                    spot_fallback_source=tasty_spot_lookup,
                    futures_cvd=futures_cvd,
                )
                setup_cooldown.succeeded(symbol)
            except ConnectionError as exc:
                # Spojení se rozpadlo uprostřed setupu (odhlášená TWS, restart
                # Gateway). O symbolu to nevypovídá nic — cooldown by jen držel
                # graf prázdný, i když se supervisor za pár vteřin přepojí (#455).
                logger.warning("Setup %s přerušen výpadkem spojení: %s", symbol, exc)
            except InstrumentSetupError as exc:
                delay = setup_cooldown.penalize(symbol)
                logger.warning("Setup %s selhal (další pokus za %d cyklů): %s", symbol, delay, exc)
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
                delay = setup_cooldown.penalize(symbol)
                logger.exception(
                    "Setup %s selhal neočekávaně — další pokus za %d cyklů", symbol, delay
                )
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
        # Účty čte ib_async z připojení; po přepojení se mohou změnit (#446)
        account = classify_accounts(ib.managedAccounts())
        # Status se pushuje i BEZ pipelines (#756). Dřív ho podmiňoval neprázdný
        # `results`, takže při nepřipojeném IBKR neodešel vůbec a UI hlásilo
        # mrtvý engine — přestože engine běžel a tastytrade větev dodávala cenu.
        # „Engine běží" a „IBKR je připojen" jsou dva různé stavy a status je
        # nesmí splácnout dohromady.
        if full_run:
            # Dohled disku (#773): měří se v intervalu DiskWatch, mezi měřeními
            # se vrací poslední snímek — rglob a SQL neběží každou minutu
            disk_snapshot = await asyncio.to_thread(disk_watch.tick, utcnow_ts())
            if disk_snapshot is not None:
                disk_alert = disk_watch.evaluate(disk_snapshot)
                if disk_alert is not None:
                    await publisher.publish(
                        "alerts",
                        {
                            "kind": "disk_space",
                            "symbol": "*",
                            "message": disk_alert.message,
                            "ts": now.timestamp(),
                        },
                    )
            await publisher.status(
                engine="online",
                connection=manager.state.value,
                port=settings.ibkr_port,
                last_tick_ts=now.isoformat(),
                # Kolikrát TWS za běh odmítla market data (#417) — s platnými
                # subskripcemi má zůstat na nule, růst je signál k prověření.
                # Okno + záznamy (#772): kumulativ od startu nemá měřítko a bez
                # záznamů se „23" nedalo potvrdit ani vyvrátit
                subscription_errors=subscription_errors.total,
                subscription_errors_60m=subscription_errors.window_count(time.monotonic()),
                subscription_errors_excused=subscription_errors.excused,
                subscription_error_recent=[
                    {
                        "ts": dt.datetime.fromtimestamp(rec.ts, tz=dt.UTC).isoformat(),
                        "contract": rec.contract,
                        "symbol": rec.symbol,
                    }
                    for rec in subscription_errors.recent_records()
                ],
                # Připojený účet (#446): uživatel musí poznat paper od živého
                account=account.label,
                account_paper=account.paper,
                # Špička obsazených linek od minulého statusu (#630) — měřeno,
                # ne konfigurační odhad; strop účtu je tvrdých 100 (ADR-0001)
                lines_utilization=line_gauge.utilization(settings.market_data_lines),
                # Křížová kontrola feedů (#517 A) — chybí, když shadow neběží
                **_crosscheck_status(crosscheck),
                # Stav tastytrade větve (#706) — pole chybí, když větev neběží
                **(tasty_status_fields() if tasty_status_fields is not None else {}),
                # Délka výpadku IBKR spojení (#770) — chybí, když spojení drží
                **_connection_offline_status(manager),
                **_chain_source_status(chain_fallback),
                **_spot_source_status(run_list),
                # Obsazení disku (#773) — plní i patičku UI, která do teď
                # ukazovala `disk — / —`; klíče chybí do prvního měření
                **disk_watch.status_fields(int(settings.disk_limit_gb * 1024**3)),
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


async def _guarded_main() -> None:
    """Zaručený konec procesu po fatální výjimce v main() (#779).

    19. 8. dvakrát po sobě: neošetřená výjimka → asyncio teardown čekal 300 s
    na join executor vláken, pak PID 1 dál běžel (12 vláken, mrtvý engine) —
    kontejner „Up", restart policy bez šance. `os._exit` PŘÍMO z běžící smyčky
    ten teardown přeskočí úplně: parquet zápisy jsou atomické (tmp+rename) a
    PG transakční, takže tvrdý konec nic nepoškodí a restart je okamžitý.
    """
    try:
        await main()
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise
    except BaseException:
        logger.critical(
            "Fatální výjimka v main() — vynucený exit 1, ať zabere restart policy (#779)",
            exc_info=True,
        )
        logging.shutdown()
        os._exit(1)


if __name__ == "__main__":
    exit_code = 0
    try:
        asyncio.run(_guarded_main())
    except KeyboardInterrupt:
        exit_code = 130
    except BaseException:
        # Sem dojde jen výjimka z asyncio.run teardownu — main() kryje guard výš
        logger.critical("Fatální výjimka při ukončování smyčky (#779)", exc_info=True)
        exit_code = 1
    # Pojistka i pro čistý návrat: ne-daemon vlákna (ib_async, psycopg LISTEN)
    # uměla držet interpreter naživu i poté, co main() skončil
    logging.shutdown()
    os._exit(exit_code)
