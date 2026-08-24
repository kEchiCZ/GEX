"""Testy minutového feature logu (#796)."""

import datetime as dt
from pathlib import Path
from typing import cast

import pyarrow.parquet as pq
from sqlalchemy import create_engine
from test_setups import TS, FakeRuntime, RecordingPublisher

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.runtime import EngineRuntime
from gexlens_engine.setups import SetupEngine
from gexlens_engine.storage.oi_archive import OIEodRepository
from gexlens_engine.storage.parquet_store import SnapshotWriter
from gexlens_engine.storage.setups_store import SetupsRepository


def make_engine(tmp_path: Path) -> tuple[SetupEngine, Settings]:
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'setups.sqlite'}")
    repository = SetupsRepository(db)
    repository.ensure_schema()
    oi_repo = OIEodRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'oi.sqlite'}"))
    oi_repo.ensure_schema()
    settings = Settings(data_dir=tmp_path / "data")
    engine = SetupEngine(
        symbol="ES",
        repository=repository,
        oi_repository=oi_repo,
        publisher=RecordingPublisher(),
        feature_writer=SnapshotWriter(settings),
    )
    return engine, settings


def bar(o: float, h: float, low: float, c: float) -> Bar:
    return Bar(ts=TS, open=o, high=h, low=low, close=c, volume=100.0)


async def test_feature_log_writes_minute_rows(tmp_path: Path) -> None:
    engine, settings = make_engine(tmp_path)
    runtime = cast(EngineRuntime, FakeRuntime())

    for i in range(3):
        await engine.on_minute(
            TS + dt.timedelta(minutes=i), 7505, [bar(7505, 7506, 7504, 7505)], runtime
        )

    path = settings.derived_dir / "ES" / "features" / f"{TS.date().isoformat()}.parquet"
    table = pq.read_table(path)
    assert table.num_rows == 3
    row = table.to_pylist()[0]
    assert row["close"] == 7505.0
    assert row["flip"] == 7515.0  # z FakeRuntime levels
    assert row["expiry"] == "20991231"
    assert row["band_sharpness"] is None  # FakeRuntime nemá profil — bez lhaní
    # ATR chce lookback+1 minut historie; první minuty jsou poctivě NULL
    assert row["atr"] is None


async def test_feature_log_atr_po_plne_historii(tmp_path: Path) -> None:
    """Regrese na deque slicing: ATR spadl až s historií > lookback (#796).

    Krátká historie prošla přes časný `return None` v average_true_range,
    takže původní testy bug neviděly — produkce padala každou minutu.
    """
    engine, settings = make_engine(tmp_path)
    runtime = cast(EngineRuntime, FakeRuntime())

    for i in range(16):
        await engine.on_minute(
            TS + dt.timedelta(minutes=i), 7505, [bar(7505, 7506, 7504, 7505)], runtime
        )

    path = settings.derived_dir / "ES" / "features" / f"{TS.date().isoformat()}.parquet"
    rows = pq.read_table(path).to_pylist()
    assert len(rows) == 16  # žádná minuta nespadla
    assert rows[-1]["atr"] is not None and rows[-1]["atr"] > 0  # plná historie → ATR


async def test_feature_log_upserts_same_minute(tmp_path: Path) -> None:
    """Restart uprostřed dne nesmí vyrobit duplicitní řádek téže minuty."""
    engine, settings = make_engine(tmp_path)
    runtime = cast(EngineRuntime, FakeRuntime())

    await engine.on_minute(TS, 7505, [bar(7505, 7506, 7504, 7505)], runtime)
    await engine.on_minute(TS, 7506, [bar(7505, 7507, 7504, 7506)], runtime)

    path = settings.derived_dir / "ES" / "features" / f"{TS.date().isoformat()}.parquet"
    table = pq.read_table(path)
    assert table.num_rows == 1
    assert table.to_pylist()[0]["close"] == 7506.0  # vyhrává poslední zápis


async def test_feature_log_disabled_writes_nothing(tmp_path: Path) -> None:
    engine, settings = make_engine(tmp_path)
    engine = SetupEngine(
        symbol="ES",
        repository=engine.repository,
        oi_repository=engine.oi_repository,
        publisher=engine.publisher,
        feature_writer=None,
    )
    runtime = cast(EngineRuntime, FakeRuntime())

    await engine.on_minute(TS, 7505, [bar(7505, 7506, 7504, 7505)], runtime)

    assert not (settings.derived_dir / "ES" / "features").exists()
