"""Testy multi-instrument vrstvy (ADR-0003): plánování, watchlist, pipeline nad mocky."""

import asyncio
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pandas as pd
import pytest
from sqlalchemy import create_engine, insert

from gexlens_engine.compute.cumdelta import CumDeltaTracker
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import (
    ChainDiscovery,
    ExpiryInfo,
    OptionContractSpec,
    Underlying,
    build_contracts,
    select_band,
)
from gexlens_engine.ibkr.mock import MockIB, MockOIFetcher, MockQuoteStreamer
from gexlens_engine.ibkr.scheduler import SubscriptionScheduler, SweepMetrics
from gexlens_engine.ibkr.underlying import Bar
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
from gexlens_engine.setups import SetupEngine
from gexlens_engine.storage.meta import settings_table, watchlist_table
from gexlens_engine.storage.oi_archive import OIArchiver, OIEodRepository, OIRecord
from gexlens_engine.storage.parquet_store import SnapshotWriter
from gexlens_engine.storage.setups_store import SetupsRepository

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


async def test_bars_stall_alert_and_recovery_backfill(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#221: bary nechodí při živém spotu → alert; po návratu recovery + re-backfill."""
    settings, writer, repository, publisher = env
    settings.bars_stall_alert_minutes = 2
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    backfills: list[bool] = []

    async def fake_backfill() -> None:
        backfills.append(True)

    pipeline.backfill_today = fake_backfill
    ticker = pipeline.ticker
    assert isinstance(ticker, FakeTicker)

    # Cyklus 0: první spot (žádný předchozí) → pohyb neznámý, čítač stojí
    await pipeline.run_minute(TS)
    # Cykly 1–2: spot žije, bary žádné → po prahu 2 min alert
    ticker.last = 7601.0
    await pipeline.run_minute(TS + dt.timedelta(minutes=1))
    ticker.last = 7602.0
    await pipeline.run_minute(TS + dt.timedelta(minutes=2))

    alerts = [data for channel, data in publisher.messages if channel == "alerts"]
    assert [a["kind"] for a in alerts] == ["bars_stalled"]
    assert alerts[0]["symbol"] == "ES"

    # Další tichý cyklus alert neopakuje (anti-spam)
    ticker.last = 7603.0
    await pipeline.run_minute(TS + dt.timedelta(minutes=3))
    alerts = [data for channel, data in publisher.messages if channel == "alerts"]
    assert len(alerts) == 1

    # Návrat barů: recovery alert + re-backfill dnešního dne na pozadí
    ticker.last = 7604.0
    now = TS + dt.timedelta(minutes=4)
    pipeline.minute_bars.append(
        Bar(ts=now, open=7603.0, high=7605.0, low=7602.0, close=7604.0, volume=10.0)
    )
    await pipeline.run_minute(now)
    assert pipeline._backfill_task is not None
    await pipeline._backfill_task

    alerts = [data for channel, data in publisher.messages if channel == "alerts"]
    assert [a["kind"] for a in alerts] == ["bars_stalled", "bars_recovered"]
    assert backfills == [True]


async def test_no_stall_alert_when_market_quiet(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#221: zavřený trh (spot stojí) — chybějící bary nesmí spouštět alert."""
    settings, writer, repository, publisher = env
    settings.bars_stall_alert_minutes = 2
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)

    for minute in range(5):  # FakeTicker drží stejnou cenu → spot se nehýbe
        await pipeline.run_minute(TS + dt.timedelta(minutes=minute))

    assert not [data for channel, data in publisher.messages if channel == "alerts"]


async def test_vol_concentration_alert_once_per_leader(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """#208: dominantní strana příští expirace → jeden alert; nový leader → další."""
    from types import SimpleNamespace

    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)

    def sec_spec(strike: float, right: str) -> OptionContractSpec:
        return OptionContractSpec("ES", "FOP", "20260718", strike, right, "CME", "E4D", "50")

    def cached(volume: float) -> SimpleNamespace:
        return SimpleNamespace(snapshot=SimpleNamespace(volume=volume))

    quotes = {
        sec_spec(7450.0, "P"): cached(4100),
        sec_spec(7500.0, "P"): cached(900),
        sec_spec(7580.0, "C"): cached(800),
        sec_spec(7400.0, "P"): cached(700),
    }
    pipeline.next_runtime = SimpleNamespace(  # type: ignore[assignment]
        expiry="20260718", scheduler=SimpleNamespace(quotes=lambda: quotes)
    )

    await pipeline._check_vol_concentration(TS)
    await pipeline._check_vol_concentration(TS)  # anti-spam: týž leader jen jednou

    alerts = [
        data
        for channel, data in publisher.messages
        if channel == "alerts" and data["kind"] == "vol_concentration"
    ]
    assert len(alerts) == 1
    first = str(alerts[0]["message"])
    assert "7450P" in first
    assert "pojistka" in first  # put pod spotem 7600 → dovětek

    quotes[sec_spec(7650.0, "C")] = cached(20000)  # nový dominantní leader
    await pipeline._check_vol_concentration(TS)
    alerts = [
        data
        for channel, data in publisher.messages
        if channel == "alerts" and data["kind"] == "vol_concentration"
    ]
    assert len(alerts) == 2
    second = str(alerts[1]["message"])
    assert "7650C" in second
    assert "strop" in second  # call nad spotem → dovětek


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


async def test_setup_selfcheck_alerts_on_drawdown_and_recovers(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher], tmp_path: Path
) -> None:
    """#309: prodělávající detektor se ozve sám; zdravý stav zvonek neruší."""
    settings, writer, repository, publisher = env
    pipeline = make_pipeline("ES", 7600.0, settings, writer, repository, publisher)
    setups = SetupsRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}"))
    setups.ensure_schema()
    pipeline.setup_engine = cast(SetupEngine, SimpleNamespace(repository=setups, on_minute=None))

    today = TS.date()

    def add(status: str, outcome_r: float, *, minutes: int) -> None:
        setup_id = setups.create(
            symbol="ES",
            expiry="20260717",
            template="failed_break",
            direction="short",
            created_ts=TS,
            entry=7600.0,
            target=7580.0,
            stop=7610.0,
            confidence=55,
            reason="test",
            context={},
        )
        setups.close(
            setup_id,
            status=status,
            closed_ts=TS + dt.timedelta(minutes=minutes),
            outcome_r=outcome_r,
            mfe=0.0,
            mae=0.0,
        )

    # 15 stopů = ΣR −15 pod prahem −10 při dostatku vzorků
    for i in range(15):
        add("closed_stop", -1.0, minutes=i)
    await pipeline._run_setup_selfcheck(today)
    alerts = [d for ch, d in publisher.messages if ch == "alerts"]
    assert alerts[-1]["kind"] == "setup_degraded"
    assert "failed_break short" in str(alerts[-1]["message"])

    # Druhý běh téhož dne nic neposílá (jednou za den) …
    before = len(publisher.messages)
    await pipeline._run_setup_selfcheck(today)
    assert len(publisher.messages) == before
    # …a opakované zhoršení další den taky ne (alert právě jednou)
    await pipeline._run_setup_selfcheck(today + dt.timedelta(days=1))
    assert len(publisher.messages) == before

    # Návrat nad práh → recovered právě jednou
    for i in range(30):
        add("closed_target", 1.5, minutes=100 + i)
    await pipeline._run_setup_selfcheck(today + dt.timedelta(days=2))
    alerts = [d for ch, d in publisher.messages if ch == "alerts"]
    assert alerts[-1]["kind"] == "setup_recovered"
    before = len(publisher.messages)
    await pipeline._run_setup_selfcheck(today + dt.timedelta(days=3))
    assert len(publisher.messages) == before


def test_closed_since_ignores_active_and_older_window(tmp_path: Path) -> None:
    """#309: okno se řídí časem uzavření, běžící setupy do bilance nepatří."""
    setups = SetupsRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}"))
    setups.ensure_schema()

    def new_setup() -> int:
        return setups.create(
            symbol="ES",
            expiry="20260717",
            template="wall_bounce",
            direction="long",
            created_ts=TS - dt.timedelta(days=30),  # vznik dávno před oknem
            entry=7600.0,
            target=7620.0,
            stop=7590.0,
            confidence=55,
            reason="test",
            context={},
        )

    inside = new_setup()
    setups.close(inside, status="closed_target", closed_ts=TS, outcome_r=2.0, mfe=0.0, mae=0.0)
    outside = new_setup()
    setups.close(
        outside,
        status="closed_stop",
        closed_ts=TS - dt.timedelta(days=10),
        outcome_r=-1.0,
        mfe=0.0,
        mae=0.0,
    )
    new_setup()  # zůstává active

    rows = setups.closed_since("ES", TS - dt.timedelta(days=7))
    assert len(rows) == 1
    assert rows[0].outcome_r == 2.0
    assert setups.closed_since("NQ", TS - dt.timedelta(days=7)) == []


async def test_secondary_expiry_band_expands_with_price(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """Obálka sekundární expirace musí sledovat cenu (#442).

    Do #442 ji `create_pipeline` nastavil jednou při startu a `maybe_expand` se
    na ni nevolal — 3. 8. tak zamrzla na 28290–28680, cena vyrostla na 28925
    a heatmapa i zdi následující expirace se nad horním strikem přestaly kreslit.
    """
    settings, writer, oi_repository, publisher = env
    spot = 7500.0
    pipeline = make_pipeline("ES", spot, settings, writer, oi_repository, publisher)

    # Sekundár se stejnou nabídkou strikes jako aktivní řetěz, ale širší osou
    strikes = tuple(spot + offset for offset in range(-400, 401, 10))
    next_info = ExpiryInfo(
        trading_class="ES1",
        expiry="20260718",
        exchange="CME",
        multiplier="50",
        strikes=strikes,
    )
    next_band = select_band(next_info.strikes, spot, settings.strike_range_points)
    underlying = Underlying(symbol="ES", sec_type="FUT", exchange="CME", con_id=1)
    pipeline.next_info = next_info
    pipeline.next_band = next_band
    pipeline.next_runtime = EngineRuntime(
        settings=settings,
        scheduler=SubscriptionScheduler(MockQuoteStreamer(), settings),
        writer=writer,
        oi_repository=oi_repository,
        publisher=publisher,
        symbol="ES",
        expiry=next_info.expiry,
        multiplier=50.0,
        contracts=build_contracts(underlying, next_info, next_band),
        cum_delta=CumDeltaTracker(multiplier=50.0),
        push_status=False,
        secondary=True,
    )
    original_high = next_band.high

    # Cena vyběhne k hornímu okraji pásma — stejná situace jako 3. 8.
    pipeline._expand_secondary(spot=original_high - 5.0)

    assert pipeline.next_band is not None
    assert pipeline.next_band.high > original_high
    # Kontrakty sekundáru se musí přestavět, jinak se nová šířka nikam nepropíše
    assert max(c.strike for c in pipeline.next_runtime.contracts) > original_high


async def test_secondary_expansion_noop_without_secondary_runtime(
    env: tuple[Settings, SnapshotWriter, OIEodRepository, RecordingPublisher],
) -> None:
    """Bez sekundáru (vypnutý sweep_next_expiry) se nesmí nic stát."""
    settings, writer, oi_repository, publisher = env
    pipeline = make_pipeline("ES", 7500.0, settings, writer, oi_repository, publisher)

    pipeline._expand_secondary(spot=9999.0)  # nesmí spadnout

    assert pipeline.next_band is None
