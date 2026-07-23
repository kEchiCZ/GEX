"""Smoke test runtime (issue #30): jeden cyklus nad mocky vyprodukuje kompletní den."""

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec
from gexlens_engine.ibkr.mock import MockQuoteStreamer
from gexlens_engine.ibkr.scheduler import SubscriptionScheduler
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord
from gexlens_engine.storage.parquet_store import SnapshotWriter

TS = dt.datetime(2026, 7, 16, 15, 0, tzinfo=dt.UTC)
SPOT = 7600.0


class RecordingPublisher(PublisherLike):
    def __init__(self) -> None:
        self.statuses: list[dict[str, object]] = []
        self.messages: list[tuple[str, dict[str, object]]] = []

    async def status(self, **fields: object) -> None:
        self.statuses.append(fields)

    async def publish(self, channel: str, data: dict[str, object]) -> None:
        self.messages.append((channel, data))


def contracts() -> list[OptionContractSpec]:
    return [
        OptionContractSpec("ES", "FOP", "20260716", strike, right, "CME", "E3D", "50")
        for strike in (7590.0, 7600.0, 7610.0)
        for right in ("C", "P")
    ]


@pytest.fixture
def runtime(tmp_path: Path) -> tuple[EngineRuntime, RecordingPublisher, Settings]:
    settings = Settings(data_dir=tmp_path / "data")
    specs = contracts()
    repository = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}"))
    repository.ensure_schema()
    repository.upsert_many(
        [OIRecord("ES", "20260716", s.strike, s.right, TS.date(), 1000.0) for s in specs]
    )
    publisher = RecordingPublisher()
    engine_runtime = EngineRuntime(
        settings=settings,
        scheduler=SubscriptionScheduler(MockQuoteStreamer(), settings),
        writer=SnapshotWriter(settings),
        oi_repository=repository,
        publisher=publisher,
        symbol="ES",
        expiry="20260716",
        multiplier=50.0,
        contracts=specs,
    )
    return engine_runtime, publisher, settings


async def test_one_cycle_produces_full_day_artifacts(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    engine_runtime, publisher, settings = runtime
    bars = [Bar(ts=TS, open=7599.0, high=7601.0, low=7598.0, close=SPOT, volume=1200.0)]

    await engine_runtime.run_cycle(TS, SPOT, bars)

    day = TS.date().isoformat()
    snapshots = pd.read_parquet(settings.snapshots_dir / "ES" / "20260716" / f"{day}.parquet")
    assert len(snapshots) == 6
    assert snapshots["oi"].iloc[0] == 1000.0  # OI z ranního archivu

    levels = pd.read_parquet(settings.derived_dir / "ES" / "20260716" / "levels" / f"{day}.parquet")
    assert len(levels) == 1
    # Sekundární zdi jdou do vlastní řady levels2 (ADR-0008, #92)
    levels2 = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "levels2" / f"{day}.parquet"
    )
    assert list(levels2.columns) == ["ts_min", "call_wall_2", "put_wall_2"]
    assert len(levels2) == 1
    # Dominance zdí jde do vlastní řady walldom (ADR-0010, #223)
    walldom = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "walldom" / f"{day}.parquet"
    )
    assert list(walldom.columns) == [
        "ts_min",
        "call_wall_dom",
        "put_wall_dom",
        "call_wall_2_dom",
        "put_wall_2_dom",
    ]
    assert len(walldom) == 1
    flow = pd.read_parquet(settings.derived_dir / "ES" / "flow" / f"{day}.parquet")
    assert list(flow.columns) == ["ts_min", "flow_delta", "cum_delta"]
    day_bars = pd.read_parquet(settings.derived_dir / "ES" / "bars" / f"{day}.parquet")
    assert day_bars["close"].iloc[0] == SPOT

    # Push do API: status + levels + flow + price kanály
    assert publisher.statuses[-1]["engine"] == "online"
    assert publisher.statuses[-1]["greeks_complete"] == 6
    channels = [channel for channel, _ in publisher.messages]
    assert "levels.ES.20260716" in channels
    # WS levels nese dominance zdí aditivně (ADR-0010, #223)
    levels_data = next(
        data for channel, data in publisher.messages if channel == "levels.ES.20260716"
    )
    assert "call_wall_dom" in levels_data and "put_wall_dom" in levels_data
    assert "flow.ES" in channels
    assert "price.ES" in channels
    assert "snapshot.ES.20260716" in channels
    # Dyn GEX profil (ADR-0009): kanál + persistence do vlastní řady
    assert "gexprofile.ES.20260716" in channels
    gexprofile_data = next(
        data for channel, data in publisher.messages if channel == "gexprofile.ES.20260716"
    )
    assert isinstance(gexprofile_data["values"], list) and gexprofile_data["values"]
    gexprofile = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "gexprofile" / f"{day}.parquet"
    )
    assert len(gexprofile) == 1
    # Modelované pole (ADR-0009 fáze 2): kanál + partice jen s posledním stavem
    assert "gexfield.ES.20260716" in channels
    gexfield_data = next(
        data for channel, data in publisher.messages if channel == "gexfield.ES.20260716"
    )
    field_values = gexfield_data["values"]
    field_cols = gexfield_data["col_count"]
    assert isinstance(field_values, list) and field_values
    assert isinstance(field_cols, int) and field_cols > 0
    assert len(field_values) % field_cols == 0  # sloupce za sebou, celé násobky mřížky
    gexfield = pd.read_parquet(
        settings.derived_dir / "ES" / "20260716" / "gexfield" / f"{day}.parquet"
    )
    assert len(gexfield) == 1  # jen poslední stav (replace_and_write)

    # price kanál nese plnou OHLC (#127), ne jen close
    price_data = next(data for channel, data in publisher.messages if channel == "price.ES")
    assert price_data["open"] == 7599.0
    assert price_data["high"] == 7601.0
    assert price_data["low"] == 7598.0
    assert price_data["close"] == SPOT
    assert price_data["volume"] == 1200.0
    assert price_data["final"] is True  # uzavřený bar (ADR-0005)

    # levels kanál nese i sekundární zdi (aditivní pole, ADR-0008)
    levels_data = next(
        data for channel, data in publisher.messages if channel == "levels.ES.20260716"
    )
    assert "call_wall_2" in levels_data
    assert "put_wall_2" in levels_data

    # snapshot kanál nese per-strike řez minuty (#127)
    snap_data = next(
        data for channel, data in publisher.messages if channel == "snapshot.ES.20260716"
    )
    snap_rows = snap_data["rows"]
    assert isinstance(snap_rows, list) and len(snap_rows) == 6
    assert set(snap_rows[0]) >= {"strike", "right", "oi", "volume", "delta", "stale_age"}
    assert snap_rows[0]["oi"] == 1000.0


async def test_forming_bar_published_and_written_as_provisional(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    """ADR-0005: rozdělaná minuta má svíčku hned, ne až po dalším cyklu."""
    engine_runtime, publisher, settings = runtime
    # Cyklus minuty TS: uzavřený bar patří PŘEDCHOZÍ minutě, rozdělaný té aktuální
    closed = Bar(ts=TS - dt.timedelta(minutes=1), open=7590.0, high=7595.0, low=7589.0,
                 close=7594.0, volume=800.0)  # fmt: skip
    forming = Bar(ts=TS, open=7594.0, high=7602.0, low=7593.0, close=SPOT, volume=310.0)

    await engine_runtime.run_cycle(TS, SPOT, [closed], forming)

    prices = [data for channel, data in publisher.messages if channel == "price.ES"]
    assert len(prices) == 2
    assert prices[0]["ts"] == (TS - dt.timedelta(minutes=1)).isoformat()
    assert prices[0]["final"] is True
    assert prices[1]["ts"] == TS.isoformat()
    assert prices[1]["final"] is False
    assert prices[1]["close"] == SPOT

    # Obě minuty jsou i v partici, aby je dostal REST balík po refreshi
    day = TS.date().isoformat()
    bars = pd.read_parquet(settings.derived_dir / "ES" / "bars" / f"{day}.parquet")
    assert len(bars) == 2
    assert list(bars.sort_values("ts_min")["close"]) == [7594.0, SPOT]


async def test_final_bar_replaces_provisional_without_duplicate(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    """ADR-0005: upsert podle ts_min — jedna minuta = jeden řádek."""
    engine_runtime, _publisher, settings = runtime
    provisional = Bar(ts=TS, open=7594.0, high=7602.0, low=7593.0, close=7598.0, volume=310.0)
    await engine_runtime.run_cycle(TS, SPOT, [], provisional)

    day = TS.date().isoformat()
    path = settings.derived_dir / "ES" / "bars" / f"{day}.parquet"
    assert len(pd.read_parquet(path)) == 1

    # Další cyklus doručí finální bar téže minuty + rozdělanou další minutu
    final = Bar(ts=TS, open=7594.0, high=7605.0, low=7590.0, close=7601.0, volume=1250.0)
    next_forming = Bar(
        ts=TS + dt.timedelta(minutes=1), open=7601.0, high=7603.0, low=7600.0,
        close=7602.0, volume=120.0,
    )  # fmt: skip
    await engine_runtime.run_cycle(TS + dt.timedelta(minutes=1), SPOT, [final], next_forming)

    bars = pd.read_parquet(path).sort_values("ts_min")
    assert len(bars) == 2  # žádný duplikát minuty TS
    assert list(bars["close"]) == [7601.0, 7602.0]  # provizorní nahrazen finálním
    assert list(bars["volume"]) == [1250.0, 120.0]


async def test_forming_bar_of_other_minute_is_ignored(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    """ADR-0005: raději žádná svíčka než svíčka pod cizím časem."""
    engine_runtime, publisher, settings = runtime
    stale_forming = Bar(
        ts=TS - dt.timedelta(minutes=3), open=1.0, high=2.0, low=0.5, close=1.5, volume=9.0
    )
    await engine_runtime.run_cycle(TS, SPOT, [], stale_forming)

    assert [data for channel, data in publisher.messages if channel == "price.ES"] == []
    day = TS.date().isoformat()
    assert not (settings.derived_dir / "ES" / "bars" / f"{day}.parquet").exists()


async def test_second_cycle_appends_and_accumulates(
    runtime: tuple[EngineRuntime, RecordingPublisher, Settings],
) -> None:
    engine_runtime, _publisher, settings = runtime
    await engine_runtime.run_cycle(TS, SPOT, [])
    await engine_runtime.run_cycle(TS + dt.timedelta(minutes=1), SPOT + 5, [])

    day = TS.date().isoformat()
    snapshots = pd.read_parquet(settings.snapshots_dir / "ES" / "20260716" / f"{day}.parquet")
    assert len(snapshots) == 12  # dvě minuty × 6 kontraktů
    levels = pd.read_parquet(settings.derived_dir / "ES" / "20260716" / "levels" / f"{day}.parquet")
    assert len(levels) == 2
