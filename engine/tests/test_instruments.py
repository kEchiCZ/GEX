"""Testy multi-instrument vrstvy (ADR-0003): plánování, watchlist, pipeline nad mocky."""

import asyncio
import datetime as dt
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, insert

from gexlens_engine.compute.cumdelta import CumDeltaTracker
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import (
    ChainDiscovery,
    ExpiryInfo,
    Underlying,
    build_contracts,
    select_band,
)
from gexlens_engine.ibkr.mock import MockIB, MockOIFetcher, MockQuoteStreamer
from gexlens_engine.ibkr.scheduler import SubscriptionScheduler, SweepMetrics
from gexlens_engine.instruments import (
    InstrumentPipeline,
    WatchlistReader,
    aggregate_status,
    gather_metrics,
    merge_symbols,
    parse_multiplier,
    plan_instruments,
)
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.storage.meta import settings_table, watchlist_table
from gexlens_engine.storage.oi_archive import OIArchiver, OIEodRepository, OIRecord
from gexlens_engine.storage.parquet_store import SnapshotWriter

TS = dt.datetime(2026, 7, 17, 15, 0, tzinfo=dt.UTC)


# ── Čisté funkce ───────────────────────────────────────────────────


def test_parse_multiplier() -> None:
    assert parse_multiplier("50") == 50.0
    assert parse_multiplier("20") == 20.0
    assert parse_multiplier("") == 1.0
    assert parse_multiplier(None) == 1.0
    assert parse_multiplier("nesmysl") == 1.0  # varování, engine nesmí spadnout


def test_merge_symbols_dedupe_uppercase_base_first() -> None:
    assert merge_symbols(["ES"], ["nq", "ES", " cl "]) == ["ES", "NQ", "CL"]
    assert merge_symbols(["ES", "NQ"], []) == ["ES", "NQ"]
    assert merge_symbols([], ["es"]) == ["ES"]


def test_expiry_expired_roll() -> None:
    from gexlens_engine.instruments import expiry_expired

    today = dt.date(2026, 7, 18)
    assert expiry_expired("20260717", today) is True  # včerejší 0DTE → roll
    assert expiry_expired("20260718", today) is False  # dnešní žije
    assert expiry_expired("20260720", today) is False
    assert expiry_expired("nesmysl", today) is False  # nečitelný formát neshazuje běh


def test_plan_instruments_start_stop_and_cap() -> None:
    plan = plan_instruments(running=["ES", "NQ"], desired=["ES", "CL"], max_instruments=3)
    assert plan.start == ["CL"]
    assert plan.stop == ["NQ"]
    assert plan.skipped == []

    # Strop: priorita = pořadí v desired (základ z konfigurace první)
    capped = plan_instruments(running=[], desired=["ES", "NQ", "CL", "GC"], max_instruments=2)
    assert capped.start == ["ES", "NQ"]
    assert capped.skipped == ["CL", "GC"]

    # Instrument nad stropem, který běžel, se zastaví
    over = plan_instruments(running=["GC"], desired=["ES", "NQ", "GC"], max_instruments=2)
    assert over.stop == ["GC"]


# ── Watchlist z DB ─────────────────────────────────────────────────


def test_watchlist_reader_roundtrip(tmp_path: Path) -> None:
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}")
    reader = WatchlistReader(db)
    reader.ensure_schema()
    assert reader.symbols() == []  # prázdná tabulka, žádná chyba
    assert reader.setting("strike_range_points") is None

    with db.begin() as conn:
        conn.execute(insert(watchlist_table).values(symbol="ES"))
        conn.execute(insert(watchlist_table).values(symbol="NQ"))
        conn.execute(insert(settings_table).values(key="strike_range_points", value=400))
    assert reader.symbols() == ["ES", "NQ"]
    assert reader.setting("strike_range_points") == 400


def test_clamp_strike_range() -> None:
    from gexlens_engine.instruments import clamp_strike_range

    settings = Settings(data_dir=Path("data"))  # default 200, max 800
    assert clamp_strike_range(400, settings) == 400.0
    assert clamp_strike_range("300", settings) == 300.0
    assert clamp_strike_range(10, settings) == 50.0  # spodní mez
    assert clamp_strike_range(9999, settings) == 400.0  # strop max/2
    assert clamp_strike_range(200, settings) is None  # beze změny
    assert clamp_strike_range("nesmysl", settings) is None
    assert clamp_strike_range(True, settings) is None
    assert clamp_strike_range({"x": 1}, settings) is None


def test_oi_prev_day_queries(tmp_path: Path) -> None:
    """ΔOI vs. včera: poslední archivovaný den před datem + hodnoty daného dne."""
    repository = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    repository.ensure_schema()
    day1, day2 = dt.date(2026, 7, 16), dt.date(2026, 7, 17)
    repository.upsert_many([OIRecord("ES", "20260717", 7500.0, "P", day1, 100.0)])
    repository.upsert_many([OIRecord("ES", "20260717", 7500.0, "P", day2, 150.0)])

    assert repository.latest_day_before("ES", "20260717", day2) == day1
    assert repository.latest_day_before("ES", "20260717", day1) is None
    values = repository.values_for("ES", "20260717", day1)
    assert len(values) == 1
    assert values[0].strike == 7500.0 and values[0].right == "P" and values[0].oi == 100.0


# ── Pipeline nad mocky ─────────────────────────────────────────────


class RecordingPublisher(PublisherLike):
    def __init__(self) -> None:
        self.statuses: list[dict[str, object]] = []
        self.messages: list[tuple[str, dict[str, object]]] = []

    async def status(self, **fields: object) -> None:
        self.statuses.append(fields)

    async def publish(self, channel: str, data: dict[str, object]) -> None:
        self.messages.append((channel, data))


class FakeTicker:
    def __init__(self, last: float) -> None:
        self.last = last

    def marketPrice(self) -> float:
        return self.last


def make_pipeline(
    symbol: str,
    spot: float,
    settings: Settings,
    writer: SnapshotWriter,
    oi_repository: OIEodRepository,
    publisher: RecordingPublisher,
    *,
    oi_available: bool = True,
) -> InstrumentPipeline:
    strikes = tuple(spot + offset for offset in (-10.0, 0.0, 10.0))
    info = ExpiryInfo(
        trading_class=f"{symbol}0",
        expiry="20260717",
        exchange="CME",
        multiplier="50",
        strikes=strikes,
    )
    underlying = Underlying(symbol=symbol, sec_type="FUT", exchange="CME", con_id=1)
    band = select_band(info.strikes, spot, settings.strike_range_points)
    contracts = build_contracts(underlying, info, band)
    oi_repository.upsert_many(
        [OIRecord(symbol, info.expiry, c.strike, c.right, TS.date(), 500.0) for c in contracts]
    )
    runtime = EngineRuntime(
        settings=settings,
        scheduler=SubscriptionScheduler(MockQuoteStreamer(), settings),
        writer=writer,
        oi_repository=oi_repository,
        publisher=publisher,
        symbol=symbol,
        expiry=info.expiry,
        multiplier=50.0,
        contracts=contracts,
        cum_delta=CumDeltaTracker(multiplier=50.0),
        push_status=False,
    )
    return InstrumentPipeline(
        symbol=symbol,
        settings=settings,
        publisher=publisher,
        discovery=ChainDiscovery(MockIB(), settings),
        info=info,
        band=band,
        runtime=runtime,
        archiver=OIArchiver(oi_repository, MockOIFetcher(), settings),
        oi_repository=oi_repository,
        ticker=FakeTicker(spot),
        minute_bars=[],
        spot=spot,
        oi_available=oi_available,
    )


@pytest.fixture
def env(tmp_path: Path) -> tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher]:
    settings = Settings(data_dir=tmp_path / "data")
    repository = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}"))
    repository.ensure_schema()
    return settings, SnapshotWriter(settings), repository, RecordingPublisher()


async def test_two_pipelines_write_separate_symbol_partitions(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    settings, writer, repository, publisher = env
    es = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    nq = make_pipeline("NQ", 24000.0, settings, writer, repository, publisher)

    results = await gather_metrics([es, nq], TS)

    day = TS.date().isoformat()
    es_rows = pd.read_parquet(settings.snapshots_dir / "ES" / "20260717" / f"{day}.parquet")
    nq_rows = pd.read_parquet(settings.snapshots_dir / "NQ" / "20260717" / f"{day}.parquet")
    assert len(es_rows) == 6 and len(nq_rows) == 6
    assert es_rows["oi"].iloc[0] == 500.0

    # Agregovaný status: součty přes instrumenty
    status = aggregate_status(results)
    assert status["greeks_total"] == 12
    assert status["greeks_complete"] == 12
    assert status["symbols"] == "ES,NQ"

    # Live kanály per symbol
    channels = [channel for channel, _ in publisher.messages]
    assert "levels.ES.20260717" in channels
    assert "levels.NQ.20260717" in channels


async def test_pipeline_failure_does_not_stop_others(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    settings, writer, repository, publisher = env
    healthy = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    broken = make_pipeline("NQ", 24000.0, settings, writer, repository, publisher)

    async def boom(now: dt.datetime) -> SweepMetrics:
        raise RuntimeError("simulovaný pád")

    broken.run_minute = boom  # type: ignore[method-assign]

    results = await gather_metrics([broken, healthy], TS)
    assert results[0] == ("NQ", None)
    assert results[1][0] == "ES" and results[1][1] is not None
    status = aggregate_status(results)
    assert status["greeks_total"] == 6  # jen zdravý instrument


async def test_next_expiry_secondary_runtime(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """Sekundární řetěz: snapshots+levels své expirace, žádný flow/bary, kadence 1/k."""
    settings, writer, repository, publisher = env
    settings.next_expiry_sweep_every = 2
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)

    next_info = ExpiryInfo(
        trading_class="EW1",
        expiry="20260720",
        exchange="CME",
        multiplier="50",
        strikes=(7590.0, 7600.0, 7610.0),
    )
    underlying = Underlying(symbol="ES", sec_type="FUT", exchange="CME", con_id=1)
    next_band = select_band(next_info.strikes, 7600.0, settings.strike_range_points)
    pipeline.next_runtime = EngineRuntime(
        settings=settings,
        scheduler=SubscriptionScheduler(MockQuoteStreamer(), settings),
        writer=writer,
        oi_repository=repository,
        publisher=publisher,
        symbol="ES",
        expiry=next_info.expiry,
        multiplier=50.0,
        contracts=build_contracts(underlying, next_info, next_band),
        push_status=False,
        secondary=True,
    )

    for minute in range(3):  # kadence 1/2 → sekundární běží v minutách 0 a 2
        await pipeline.run_minute(TS + dt.timedelta(minutes=minute))

    day = TS.date().isoformat()
    primary = pd.read_parquet(settings.snapshots_dir / "ES" / "20260717" / f"{day}.parquet")
    secondary = pd.read_parquet(settings.snapshots_dir / "ES" / "20260720" / f"{day}.parquet")
    assert len(primary) == 18  # 3 minuty × 6 kontraktů
    assert len(secondary) == 12  # 2 běhy × 6 kontraktů
    levels_next = pd.read_parquet(
        settings.derived_dir / "ES" / "20260720" / "levels" / f"{day}.parquet"
    )
    assert len(levels_next) == 2
    # Flow patří jen aktivní expiraci — 3 řádky (žádná duplikace sekundárním během)
    flow = pd.read_parquet(settings.derived_dir / "ES" / "flow" / f"{day}.parquet")
    assert len(flow) == 3
    channels = [channel for channel, _ in publisher.messages]
    assert "levels.ES.20260720" in channels


async def test_oi_missing_alert_and_retry_counter(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    settings, writer, repository, publisher = env
    # Jiný den než upsert v make_pipeline → OI archiv pro dnešek chybí
    pipeline = make_pipeline("CL", 80.0, settings, writer, repository, publisher)

    ok = await pipeline.try_archive_oi(dt.date(2026, 7, 18))  # MockOIFetcher bez hodnot
    assert ok is False
    alerts = [data for channel, data in publisher.messages if channel == "alerts"]
    assert alerts and alerts[-1]["kind"] == "oi_missing"
    assert alerts[-1]["symbol"] == "CL"


async def test_watchdog_prerusi_visici_cyklus(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#219: cyklus visící na mrtvém IBKR await nesmí zastavit orchestrátor."""
    settings, writer, repository, publisher = env
    stuck = make_pipeline("NQ", 24000.0, settings, writer, repository, publisher)
    healthy = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)

    async def hang(now: dt.datetime) -> SweepMetrics:
        await asyncio.sleep(3600)
        raise AssertionError("nedosažitelné")

    stuck.run_minute = hang  # type: ignore[method-assign]

    results = dict(await gather_metrics([stuck, healthy], TS, timeout_s=0.2))

    assert results["NQ"] is None  # timeout → neúspěšný cyklus, žádné viset
    assert results["ES"] is not None  # smyčka pokračovala dalším instrumentem


async def test_archive_failure_does_not_kill_pipeline(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#215 (MES): výjimka z archivace → alert + False, žádné probublání do cooldownu."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("MES", 7500.0, settings, writer, repository, publisher)

    class ExplodingArchiver:
        async def archive_day(self, contracts: object, day: dt.date) -> object:
            raise RuntimeError("mock: CardinalityViolation")

    pipeline.archiver = ExplodingArchiver()  # type: ignore[assignment]

    ok = await pipeline.try_archive_oi(dt.date(2026, 7, 18))

    assert ok is False  # pipeline žije dál, retry po OI_RETRY_CYCLES
    alerts = [data for channel, data in publisher.messages if channel == "alerts"]
    assert alerts and alerts[-1]["kind"] == "oi_missing"
    assert alerts[-1]["symbol"] == "MES"
