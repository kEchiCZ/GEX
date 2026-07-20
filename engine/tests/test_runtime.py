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
    flow = pd.read_parquet(settings.derived_dir / "ES" / "flow" / f"{day}.parquet")
    assert list(flow.columns) == ["ts_min", "flow_delta", "cum_delta"]
    day_bars = pd.read_parquet(settings.derived_dir / "ES" / "bars" / f"{day}.parquet")
    assert day_bars["close"].iloc[0] == SPOT

    # Push do API: status + levels + flow + price kanály
    assert publisher.statuses[-1]["engine"] == "online"
    assert publisher.statuses[-1]["greeks_complete"] == 6
    channels = [channel for channel, _ in publisher.messages]
    assert "levels.ES.20260716" in channels
    assert "flow.ES" in channels
    assert "price.ES" in channels
    assert "snapshot.ES.20260716" in channels

    # price kanál nese plnou OHLC (#127), ne jen close
    price_data = next(data for channel, data in publisher.messages if channel == "price.ES")
    assert price_data["open"] == 7599.0
    assert price_data["high"] == 7601.0
    assert price_data["low"] == 7598.0
    assert price_data["close"] == SPOT
    assert price_data["volume"] == 1200.0

    # snapshot kanál nese per-strike řez minuty (#127)
    snap_data = next(
        data for channel, data in publisher.messages if channel == "snapshot.ES.20260716"
    )
    snap_rows = snap_data["rows"]
    assert isinstance(snap_rows, list) and len(snap_rows) == 6
    assert set(snap_rows[0]) >= {"strike", "right", "oi", "volume", "delta", "stale_age"}
    assert snap_rows[0]["oi"] == 1000.0


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
