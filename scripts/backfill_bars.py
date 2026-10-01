"""Backfill 1min barů ES/NQ z IBKR historical: hluboký (#369) a oprava dnů (#1320).

Spouští se z hostu proti běžící TWS / IB Gateway (vlastní clientId — neruší engine):

    uv run python scripts/backfill_bars.py [--symbols ES,NQ] [--depth-days 730]
    uv run python scripts/backfill_bars.py --days 2026-09-10,2026-09-14
        [--replace-tasty] [--replace-wrong-contract] [--dry-run] [--port 4001]

Hluboký backfill je idempotentní a přerušitelný: dny s existující particí se
přeskakují, takže opakované spuštění doplní jen díry.

`--days` opraví vyjmenované dny, které partici už mají: doplní chybějící minuty
a s `--replace-tasty` nahradí i minuty rekonstruované z dxFeed (`tasty_candle`).
S `--replace-wrong-contract` přepíše i dřívější doplnění (`ibkr_hist`), jehož
close se od ověřeného staženého baru liší o víc než `BACKFILL_CONTRACT_TOLERANCE`
— bar jiného kontraktu zapsaný před stráží #1232; `ibkr_hist` v toleranci
zůstává. Měřené minuty (`ibkr`) nepřepíše nikdy. Kontrakt
dne se vybírá podle měřených barů partice (stráž #1232), ne podle kalendáře,
a každý zapisovaný souvislý blok minut musí sedět i na nejbližší měřené minuty
téže seance po stranách (engine mohl během dne přepnout kontrakt; minuta za
denní pauzou je jiná seance). Doplnění díry stačí jedna strana, přepis
`ibkr_hist` potřebuje obě. Dnešek a včerejšek skript odmítne: jejich partice
drží engine v paměti a pozdním zápisem by opravu přepsal.

Port je z konfigurace (`GEXLENS_IBKR_PORT`, IB Gateway 4001), jde přebít
`--port`. Bary nesou `source = ibkr_hist` jako backfill enginu (#1055).
Throttle drží IBKR limit 60 historical requestů / 10 min s rezervou.
"""

import argparse
import asyncio
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine" / "src"))

from ib_async import IB, Contract, Future  # noqa: E402

from gexlens_engine.adapters import IbHistoricalClient  # noqa: E402
from gexlens_engine.config import Settings, load_settings  # noqa: E402
from gexlens_engine.ibkr.deepbars import (  # noqa: E402
    RejectedBlock,
    bucket_by_day,
    build_plan,
    contract_candidates,
    existing_days,
    plan_day_refill,
    task_is_covered,
)
from gexlens_engine.ibkr.pacing import PacingGuard  # noqa: E402
from gexlens_engine.ibkr.underlying import (  # noqa: E402
    BACKFILL_CONTRACT_TOLERANCE,
    Bar,
    UnderlyingBackfiller,
    contract_mismatch,
)
from gexlens_engine.storage.parquet_store import (  # noqa: E402
    BAR_SOURCE_HISTORICAL,
    BUFFER_KEEP_DAYS,
    SnapshotWriter,
    StoredBar,
    read_bars,
)

logger = logging.getLogger("backfill_bars")

#: Skript běží na hostu vedle TWS / IB Gateway; `GEXLENS_IBKR_HOST` enginu
#: v kontejneru přepisuje compose.yml na host.docker.internal, proto se nepřebírá
HOST = "127.0.0.1"
#: Vlastní clientId mimo engine (prod 1, dev 2) i sondy ve `scripts/`
CLIENT_ID = 997
THROTTLE_S = 11.0  # 60 req / 10 min = 1 / 10 s; rezerva
REQUEST_TIMEOUT_S = 60.0


async def connect(port: int, settings: Settings) -> IB:
    """Spojení s vlastním clientId; shoda s enginem je chyba, ne tichý souboj."""
    if settings.ibkr_client_id == CLIENT_ID:
        raise SystemExit(
            f"clientId {CLIENT_ID} používá engine (GEXLENS_IBKR_CLIENT_ID) — změň CLIENT_ID skriptu"
        )
    ib = IB()
    # readonly: skript jen čte historii, objednávky ani účet ho nezajímají
    await ib.connectAsync(HOST, port, clientId=CLIENT_ID, timeout=15, readonly=True)
    return ib


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
                # Doplněno zpětně, ne změřeno (#1055) — jako UnderlyingBackfiller
                source=BAR_SOURCE_HISTORICAL,
            )
        )
    return bars


def settings_for(data_dir: Path | None) -> Settings:
    settings = load_settings()
    if data_dir is not None:
        settings = settings.model_copy(update={"data_dir": data_dir})
    return settings


async def run_deep(symbols: list[str], depth_days: int, settings: Settings, port: int) -> int:
    """Hluboký backfill (#369): jen dny bez partice, kontrakt podle front oken."""
    writer = SnapshotWriter(settings)
    today = dt.datetime.now(dt.UTC).date()
    plan = build_plan(symbols, depth_days, today=today)
    logger.info("Plán: %d chunků (%s, %d dní zpět)", len(plan), ",".join(symbols), depth_days)

    ib = await connect(port, settings)
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


def day_bars(settings: Settings, symbol: str, day: dt.date) -> dict[dt.datetime, StoredBar]:
    """Minuta → uložený bar partice dne (`source` NULL = živá cesta)."""
    start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.UTC)
    # read_bars bere `since < ts ≤ until`; o mikrosekundu dřív = včetně 00:00
    since = start - dt.timedelta(microseconds=1)
    until = start + dt.timedelta(days=1) - dt.timedelta(microseconds=1)
    return {bar.ts: bar for bar in read_bars(settings.derived_dir, symbol, since, until)}


async def fetch_verified_day(
    ib: IB,
    backfillers: dict[str, UnderlyingBackfiller],
    settings: Settings,
    symbol: str,
    day: dt.date,
    measured: dict[dt.datetime, float],
) -> tuple[str, list[Bar], float] | None:
    """Bary dne z kontraktu, který sedí na měřené minuty partice (#1232).

    Kandidáti jsou front podle expirace a další kvartál (`contract_candidates`);
    bez měřených minut nelze kontrakt ověřit a den se nezapisuje.
    """
    for contract_month in contract_candidates(day):
        backfiller = backfillers.get(f"{symbol}{contract_month}")
        if backfiller is None:
            contract = await resolve_contract(ib, symbol, contract_month)
            if contract is None:
                logger.warning("Kontrakt %s %s IBKR nezná — přeskakuji", symbol, contract_month)
                continue
            backfiller = UnderlyingBackfiller(
                IbHistoricalClient(ib, contract), PacingGuard(), settings
            )
            backfillers[f"{symbol}{contract_month}"] = backfiller
        try:
            # backfill_day = týž request i razítko `ibkr_hist` jako engine (#221, #1055)
            bars = await backfiller.backfill_day(symbol, day)
        except Exception:
            logger.exception("%s %s z %s: request selhal", symbol, day, contract_month)
            bars = []
        await asyncio.sleep(THROTTLE_S)
        if not bars:
            logger.warning("%s %s z %s: IBKR nevrátil žádný bar", symbol, day, contract_month)
            continue
        deviation = contract_mismatch(measured, bars)
        if deviation is None:
            logger.error(
                "%s %s: v partici chybí měřené minuty — kontrakt nelze ověřit, den se nezapisuje",
                symbol,
                day,
            )
            return None
        if deviation > BACKFILL_CONTRACT_TOLERANCE:
            logger.warning(
                "%s %s z %s: odchylka od měřených %.2f %% — jiný kontrakt, zkouším další",
                symbol,
                day,
                contract_month,
                deviation * 100,
            )
            continue
        return contract_month, bars, deviation
    logger.error("%s %s: žádný kandidát kontraktu nesedí na měřené bary", symbol, day)
    return None


def reject_message(block: RejectedBlock, contract_month: str) -> str:
    """Důvod odmítnutí bloku pro výpis (`RejectReason`)."""
    if block.reason == "no_edge" or block.deviation is None:
        return "po stranách žádná měřená minuta téže seance, kontrakt nelze ověřit"
    if block.reason == "mismatch":
        return (
            f"měřené minuty vedle bloku se liší o {block.deviation * 100:.2f} % "
            f"(jiný kontrakt než {contract_month}?)"
        )
    return (
        "přepis ibkr_hist potřebuje měřenou minutu téže seance po obou stranách, "
        f"má jen jednu (odchylka {block.deviation * 100:.2f} %)"
    )


async def run_days(
    symbols: list[str],
    days: list[dt.date],
    settings: Settings,
    port: int,
    *,
    replace_tasty: bool,
    replace_wrong_contract: bool,
    dry_run: bool,
) -> int:
    """Oprava vyjmenovaných dnů (#1320): díry, volitelně rekonstrukce `tasty_candle`
    a dřívější doplnění `ibkr_hist` z jiného kontraktu."""
    writer = SnapshotWriter(settings)
    ib = await connect(port, settings)
    backfillers: dict[str, UnderlyingBackfiller] = {}
    failed = 0
    rejected = 0
    totals = {"filled": 0, "replaced": 0, "tasty_left": 0, "rewritten": 0}
    try:
        for symbol in symbols:
            for day in days:
                measured = writer.measured_bar_closes(symbol, day)
                fetched = await fetch_verified_day(ib, backfillers, settings, symbol, day, measured)
                if fetched is None:
                    failed += 1
                    continue
                contract_month, bars, deviation = fetched
                plan = plan_day_refill(
                    day_bars(settings, symbol, day),
                    bars,
                    measured=measured,
                    replace_tasty=replace_tasty,
                    replace_wrong_contract=replace_wrong_contract,
                )
                for block in plan.rejected:
                    logger.error(
                        "%s %s: blok %s–%s (%d min) se nezapíše — %s",
                        symbol,
                        day,
                        block.start.strftime("%H:%M"),
                        block.end.strftime("%H:%M"),
                        block.minutes,
                        reject_message(block, contract_month),
                    )
                rejected += len(plan.rejected)
                for rewrite in plan.rewritten:
                    logger.info(
                        "%s %s: blok %s–%s (%d min) ibkr_hist jiného kontraktu se přepíše barem "
                        "%s (odchylka až %.2f %%)",
                        symbol,
                        day,
                        rewrite.start.strftime("%H:%M"),
                        rewrite.end.strftime("%H:%M"),
                        rewrite.minutes,
                        contract_month,
                        rewrite.deviation * 100,
                    )
                rewritten = sum(rewrite.minutes for rewrite in plan.rewritten)
                logger.info(
                    "%s %s z %s (odchylka %.3f %%, %d barů): doplní %d, nahradí tasty %d, "
                    "tasty zůstává %d, přepíše jiný kontrakt %d%s",
                    symbol,
                    day,
                    contract_month,
                    deviation * 100,
                    len(bars),
                    plan.filled,
                    plan.replaced,
                    plan.tasty_left,
                    rewritten,
                    " — dry-run, nic se nezapisuje" if dry_run else "",
                )
                totals["filled"] += plan.filled
                totals["replaced"] += plan.replaced
                totals["tasty_left"] += plan.tasty_left
                totals["rewritten"] += rewritten
                if dry_run or not plan.bars:
                    continue
                writer.write_bars(symbol, day, plan.bars)
                # Pojistka pravidla: měřená minuta se zápisem nesmí změnit
                if writer.measured_bar_closes(symbol, day) != measured:
                    raise RuntimeError(f"{symbol} {day}: zápis změnil měřené minuty — zastavuji")
    finally:
        ib.disconnect()

    logger.info(
        "Hotovo%s: doplněno %d, nahrazeno tasty %d, tasty zůstává %d, přepsáno jiného "
        "kontraktu %d, %d dnů bez zápisu (chyba), %d bloků odmítnuto (kontrakt bloku neověřen)",
        " (dry-run)" if dry_run else "",
        totals["filled"],
        totals["replaced"],
        totals["tasty_left"],
        totals["rewritten"],
        failed,
        rejected,
    )
    return 1 if failed or rejected else 0


def parse_days(raw: str) -> list[dt.date]:
    try:
        return sorted({dt.date.fromisoformat(part.strip()) for part in raw.split(",") if part})
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"den ve tvaru YYYY-MM-DD: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill 1min barů z IBKR (#369, #1320)")
    parser.add_argument("--symbols", default="ES,NQ")
    parser.add_argument("--depth-days", type=int, default=730)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument(
        "--port", type=int, default=None, help="výchozí GEXLENS_IBKR_PORT (IB Gateway 4001)"
    )
    parser.add_argument(
        "--days", type=parse_days, default=None, help="opravit dny YYYY-MM-DD[,…] (#1320)"
    )
    parser.add_argument(
        "--replace-tasty",
        action="store_true",
        help="s --days nahradit i minuty tasty_candle barem IBKR historical",
    )
    parser.add_argument(
        "--replace-wrong-contract",
        action="store_true",
        help="s --days přepsat i minuty ibkr_hist, které nesedí na ověřený kontrakt dne",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="s --days jen vypsat, co by se zapsalo"
    )
    args = parser.parse_args()
    if args.days is None and (args.replace_tasty or args.replace_wrong_contract or args.dry_run):
        parser.error("--replace-tasty, --replace-wrong-contract a --dry-run patří k --days")
    # Partice dneška a včerejška drží engine v paměti (BUFFER_KEEP_DAYS) a jeho
    # pozdní zápis (finalizace 23:59, gap-fill, díra setupu) by opravu přepsal
    newest = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=BUFFER_KEEP_DAYS + 1)
    if args.days is not None and (not args.days or args.days[-1] > newest):
        parser.error(
            f"--days: aspoň jeden den a nejpozději {newest} (UTC) — "
            "dnešek a včerejšek drží v paměti engine"
        )

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # ib_async.wrapper loguje na INFO číslo účtu a pozice portfolia — do logu nepatří
    logging.getLogger("ib_async.wrapper").setLevel(logging.WARNING)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    settings = settings_for(args.data_dir)
    port = args.port if args.port is not None else settings.ibkr_port
    if args.days is None:
        return asyncio.run(run_deep(symbols, args.depth_days, settings, port))
    return asyncio.run(
        run_days(
            symbols,
            args.days,
            settings,
            port,
            replace_tasty=args.replace_tasty,
            replace_wrong_contract=args.replace_wrong_contract,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
