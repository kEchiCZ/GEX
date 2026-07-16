"""Vstupní bod enginu: `python -m gexlens_engine` (SPEC kap. 8, headless u IB Gateway).

Sestaví produkční závislosti (ib_async, PostgreSQL, Parquet, HTTP publisher),
objeví řetězec a spustí minutovou smyčku. Ranní OI archiv se doplní při startu,
noční retention purge běží podle konfigurovaného času.
"""

import asyncio
import datetime as dt
import logging
import os
import sys

from ib_async import IB, Future
from sqlalchemy import create_engine

from gexlens_engine.adapters import HttpPublisher, IbOIFetcher, IbQuoteStreamer
from gexlens_engine.compute.cumdelta import CumDeltaTracker
from gexlens_engine.config import ConfigError, load_settings
from gexlens_engine.ibkr.connection import ConnectionManager, ConnectionState
from gexlens_engine.ibkr.discovery import ChainDiscovery, Underlying, build_contracts
from gexlens_engine.ibkr.scheduler import SubscriptionScheduler
from gexlens_engine.ibkr.underlying import Bar, RealTimeBarAggregator
from gexlens_engine.runtime import EngineRuntime
from gexlens_engine.storage.oi_archive import OIArchiver, OIEodRepository
from gexlens_engine.storage.parquet_store import SnapshotWriter
from gexlens_engine.storage.retention import RetentionJob

logger = logging.getLogger("gexlens.engine")

ES_MULTIPLIER = 50.0


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
    manager = ConnectionManager(ib, settings)
    await manager.start()
    while manager.state is not ConnectionState.CONNECTED:
        await asyncio.sleep(0.5)

    # Discovery: front ES future → nejbližší expirace → kontrakty denní obálky.
    # Sec-def farm může být po výpadku TWS↔IB chvíli nedostupná → timeout + retry
    # (SPEC kap. 8: odolnost — start nesmí viset donekonečna).
    while True:
        try:
            details = await asyncio.wait_for(
                ib.reqContractDetailsAsync(Future("ES", exchange="CME")), timeout=30.0
            )
            if details:
                break
            logger.warning("Discovery vrátila prázdný výsledek — zkusím znovu za 10 s")
        except TimeoutError:
            logger.warning("Discovery timeout (sec-def farm nedostupná?) — retry za 10 s")
        await asyncio.sleep(10)
    futures = sorted(
        (d.contract for d in details if d.contract is not None),
        key=lambda c: c.lastTradeDateOrContractMonth,
    )
    front = futures[0]
    fut_ticker = ib.reqMktData(front, "", False, False)
    await asyncio.sleep(3)
    spot = fut_ticker.last if fut_ticker.last == fut_ticker.last else fut_ticker.marketPrice()

    discovery = ChainDiscovery(ib, settings)
    underlying = Underlying(symbol="ES", sec_type="FUT", exchange="CME", con_id=front.conId)
    infos = await discovery.discover(underlying)
    info = infos[0]
    band = discovery.initial_band(info, spot)
    contracts = build_contracts(underlying, info, band)
    logger.info(
        "Řetězec %s %s: %d kontraktů, spot %.2f",
        info.trading_class,
        info.expiry,
        len(contracts),
        spot,
    )

    streamer = IbQuoteStreamer(ib)
    scheduler = SubscriptionScheduler(streamer, settings)
    writer = SnapshotWriter(settings)
    db = create_engine(settings.database_url)
    oi_repository = OIEodRepository(db)
    await asyncio.to_thread(oi_repository.ensure_schema)

    # Ranní OI archiv (jednou za den; idempotentní upsert)
    today = dt.datetime.now(dt.UTC).date()
    if today not in oi_repository.days("ES"):
        archiver = OIArchiver(oi_repository, IbOIFetcher(ib, streamer), settings)
        result = await archiver.archive_day(contracts, today)
        logger.info(
            "OI archiv %s: %d zapsáno, %d chybí", today, result.written, len(result.missing)
        )

    runtime = EngineRuntime(
        settings=settings,
        scheduler=scheduler,
        writer=writer,
        oi_repository=oi_repository,
        publisher=publisher,
        symbol="ES",
        expiry=info.expiry,
        multiplier=ES_MULTIPLIER,
        contracts=contracts,
        cum_delta=CumDeltaTracker(multiplier=ES_MULTIPLIER),
    )

    # Bary podkladu: 5s realtime bary → 1min agregace
    minute_bars: list[Bar] = []
    aggregator = RealTimeBarAggregator(minute_bars.append)
    rt_bars = ib.reqRealTimeBars(front, 5, "TRADES", False)
    rt_bars.updateEvent += lambda bars, has_new: aggregator.add_5s_bar(
        Bar(
            ts=bars[-1].time,
            open=bars[-1].open_,
            high=bars[-1].high,
            low=bars[-1].low,
            close=bars[-1].close,
            volume=float(bars[-1].volume),
        )
    )

    retention = RetentionJob(settings)
    last_purge_date: dt.date | None = None

    while True:
        cycle_start = asyncio.get_running_loop().time()
        now = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0)
        current_spot = fut_ticker.last if fut_ticker.last == fut_ticker.last else spot
        # Auto-rozšíření denní obálky (ADR-0002)
        expansion = discovery.maybe_expand(info, band, current_spot)
        if expansion.expanded:
            band = expansion.band
            contracts = build_contracts(underlying, info, band)
            runtime.contracts = contracts
            if expansion.capped:
                await publisher.publish(
                    "alerts",
                    {
                        "kind": "band_capped",
                        "symbol": "ES",
                        "message": "Obálka strikes na stropu — vzdálený okraj se posouvá",
                        "ts": now.timestamp(),
                    },
                )
        bars_to_write = list(minute_bars)
        minute_bars.clear()
        try:
            await runtime.run_cycle(now, current_spot, bars_to_write)
        except Exception:
            logger.exception("Cyklus selhal — pokračuji dalším (SPEC kap. 8: odolnost)")

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

        elapsed = asyncio.get_running_loop().time() - cycle_start
        await asyncio.sleep(max(1.0, 60.0 - elapsed))


if __name__ == "__main__":
    asyncio.run(main())
