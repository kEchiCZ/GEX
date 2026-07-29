"""Jednorázový hluboký backfill 1min barů ES/NQ (#369).

Spouští se z hostu proti běžící TWS (vlastní clientId — neruší engine):

    uv run python scripts/backfill_bars.py [--symbols ES,NQ] [--depth-days 730]

Idempotentní a přerušitelný: dny s existující particí se přeskakují, takže
opakované spuštění doplní jen díry. Throttle drží IBKR limit 60 historical
requestů / 10 min s rezervou.
"""

import argparse
import asyncio
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine" / "src"))

from ib_async import IB, Contract, Future  # noqa: E402

from gexlens_engine.config import load_settings  # noqa: E402
from gexlens_engine.ibkr.deepbars import (  # noqa: E402
    bucket_by_day,
    build_plan,
    existing_days,
    task_is_covered,
)
from gexlens_engine.ibkr.underlying import Bar  # noqa: E402
from gexlens_engine.storage.parquet_store import SnapshotWriter  # noqa: E402

logger = logging.getLogger("backfill_bars")

HOST, PORT, CLIENT_ID = "127.0.0.1", 7496, 997
THROTTLE_S = 11.0  # 60 req / 10 min = 1 / 10 s; rezerva
REQUEST_TIMEOUT_S = 60.0


async def resolve_contract(ib: IB, symbol: str, contract_month: str) -> Contract | None:
    """Kvartální kontrakt vč. expirovaných; None = IBKR ho už nezná (>2 roky)."""
    template = Future(symbol, lastTradeDateOrContractMonth=contract_month, exchange="CME")
    template.includeExpired = True
    details = await ib.reqContractDetailsAsync(template)
    return details[0].contract if details else None


async def fetch_chunk(ib: IB, contract: Contract, end: dt.date, duration: str) -> list[Bar]:
    """Jeden chunk 1min barů; endDateTime = půlnoc UTC dne po `end`."""
    end_dt = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(0, 0), tzinfo=dt.UTC)
    raw = await asyncio.wait_for(
        ib.reqHistoricalDataAsync(
            contract,
            endDateTime=end_dt,
            durationStr=duration,
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=False,
            formatDate=2,
        ),
        timeout=REQUEST_TIMEOUT_S,
    )
    bars: list[Bar] = []
    for item in raw:
        ts = item.date
        if not isinstance(ts, dt.datetime):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.UTC)
        bars.append(
            Bar(
                ts=ts.astimezone(dt.UTC),
                open=item.open,
                high=item.high,
                low=item.low,
                close=item.close,
                volume=float(item.volume),
            )
        )
    return bars


async def run(symbols: list[str], depth_days: int, data_dir: Path | None) -> int:
    settings = load_settings()
    if data_dir is not None:
        settings = settings.model_copy(update={"data_dir": data_dir})
    writer = SnapshotWriter(settings)
    today = dt.datetime.now(dt.UTC).date()
    plan = build_plan(symbols, depth_days, today=today)
    logger.info("Plán: %d chunků (%s, %d dní zpět)", len(plan), ",".join(symbols), depth_days)

    ib = IB()
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=15)
    contracts: dict[tuple[str, str], Contract | None] = {}
    stats = {"fetched": 0, "skipped": 0, "missing_contract": 0, "failed": 0, "days": 0}
    try:
        for index, task in enumerate(plan, 1):
            existing = existing_days(settings.derived_dir, task.symbol)
            if task_is_covered(task, existing):
                stats["skipped"] += 1
                continue
            key = (task.symbol, task.contract_month)
            if key not in contracts:
                contracts[key] = await resolve_contract(ib, task.symbol, task.contract_month)
                if contracts[key] is None:
                    logger.warning("Kontrakt %s %s IBKR nezná — mimo hloubku", *key)
            contract = contracts[key]
            if contract is None:
                stats["missing_contract"] += 1
                continue
            try:
                bars = await fetch_chunk(ib, contract, task.end, task.duration)
            except Exception:
                stats["failed"] += 1
                logger.exception(
                    "Chunk %s %s end=%s selhal — pokračuji",
                    task.symbol,
                    task.contract_month,
                    task.end,
                )
                await asyncio.sleep(THROTTLE_S)
                continue
            stats["fetched"] += 1
            for day, day_bars in sorted(bucket_by_day(bars).items()):
                # Dnešek vlastní běžící engine; hluboký backfill do něj nesahá
                if day >= today:
                    continue
                writer.write_bars(task.symbol, day, day_bars)
                stats["days"] += 1
            logger.info(
                "[%d/%d] %s %s end=%s: %d barů",
                index,
                len(plan),
                task.symbol,
                task.contract_month,
                task.end,
                len(bars),
            )
            await asyncio.sleep(THROTTLE_S)
    finally:
        ib.disconnect()

    logger.info(
        "Hotovo: %(fetched)d chunků staženo, %(skipped)d přeskočeno (pokryto), "
        "%(missing_contract)d mimo hloubku IBKR, %(failed)d chyb, %(days)d denních partic",
        stats,
    )
    return 0 if stats["fetched"] or stats["skipped"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Hluboký backfill 1min barů (#369)")
    parser.add_argument("--symbols", default="ES,NQ")
    parser.add_argument("--depth-days", type=int, default=730)
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    return asyncio.run(run(symbols, args.depth_days, args.data_dir))


if __name__ == "__main__":
    sys.exit(main())
