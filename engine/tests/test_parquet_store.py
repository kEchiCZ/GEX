"""Testy SnapshotWriteru (issue #11): schéma dle SPEC, čitelnost pandasem, atomický zápis."""

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from gexlens_engine.config import Settings
from gexlens_engine.storage.parquet_store import SnapshotRow, SnapshotWriter, TickRecord

DAY = dt.date(2026, 7, 16)

SNAPSHOT_COLUMNS = [
    "ts_min",
    "strike",
    "right",
    "bid",
    "ask",
    "last",
    "volume",
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "oi",
    "stale_age",
]


def snapshot_rows(minute: int, strikes: list[float]) -> list[SnapshotRow]:
    ts = dt.datetime(2026, 7, 16, 15, minute, tzinfo=dt.UTC)
    return [
        SnapshotRow(
            ts_min=ts,
            strike=strike,
            right=right,
            bid=10.0,
            ask=10.5,
            last=10.25,
            volume=100.0,
            iv=0.15,
            delta=0.5,
            gamma=0.01,
            theta=-0.5,
            vega=1.2,
            oi=1500.0,
            stale_age=0.0,
        )
        for strike in strikes
        for right in ("C", "P")
    ]


@pytest.fixture
def writer(tmp_path: Path) -> SnapshotWriter:
    return SnapshotWriter(Settings(data_dir=tmp_path))


def test_day_of_snapshots_readable_by_pandas(writer: SnapshotWriter, tmp_path: Path) -> None:
    strikes = [7590.0, 7595.0, 7600.0]
    for minute in range(3):  # simulovaný den po minutách
        path = writer.write_minute("ES", "20260716", DAY, snapshot_rows(minute, strikes))

    assert path == tmp_path / "snapshots" / "ES" / "20260716" / "2026-07-16.parquet"
    frame = pd.read_parquet(path)
    # AC: schéma odpovídá SPEC 5.1
    assert list(frame.columns) == SNAPSHOT_COLUMNS
    assert len(frame) == 3 * len(strikes) * 2
    assert set(frame["right"].unique()) == {"C", "P"}
    assert frame["ts_min"].dt.tz is not None  # UTC timestampy


def test_ticks_partition_schema(writer: SnapshotWriter, tmp_path: Path) -> None:
    ticks = [
        TickRecord(
            ts=dt.datetime(2026, 7, 16, 15, 0, 1, tzinfo=dt.UTC),
            con_id=899117615,
            price=15.25,
            size=2.0,
            side="buy",
        ),
        TickRecord(
            ts=dt.datetime(2026, 7, 16, 15, 0, 2, tzinfo=dt.UTC),
            con_id=899117615,
            price=15.0,
            size=1.0,
            side="sell",
        ),
    ]

    path = writer.write_ticks("ES", DAY, ticks)

    assert path == tmp_path / "ticks" / "ES" / "2026-07-16.parquet"
    frame = pd.read_parquet(path)
    assert list(frame.columns) == ["ts", "conId", "price", "size", "side"]
    assert list(frame["side"]) == ["buy", "sell"]


def test_appends_accumulate_within_day(writer: SnapshotWriter) -> None:
    path = writer.write_minute("ES", "20260716", DAY, snapshot_rows(0, [7600.0]))
    frame_1 = pd.read_parquet(path)
    writer.write_minute("ES", "20260716", DAY, snapshot_rows(1, [7600.0]))
    frame_2 = pd.read_parquet(path)

    assert len(frame_1) == 2
    assert len(frame_2) == 4
    assert frame_2["ts_min"].nunique() == 2


def test_new_writer_continues_existing_partition(tmp_path: Path) -> None:
    """Restart enginu uprostřed dne: nový writer naváže na existující partici."""
    settings = Settings(data_dir=tmp_path)
    path = SnapshotWriter(settings).write_minute("ES", "20260716", DAY, snapshot_rows(0, [7600.0]))

    SnapshotWriter(settings).write_minute("ES", "20260716", DAY, snapshot_rows(1, [7600.0]))

    frame = pd.read_parquet(path)
    assert len(frame) == 4  # obě minuty, žádná ztráta po "restartu"


def test_no_partial_files_after_simulated_crash(writer: SnapshotWriter, tmp_path: Path) -> None:
    """AC: žádné částečné soubory po kill -9 — osiřelý .tmp se ignoruje a uklidí."""
    path = writer.write_minute("ES", "20260716", DAY, snapshot_rows(0, [7600.0]))

    # Simulace kill -9 uprostřed zápisu jiného procesu: nedokončený temp soubor vedle partice
    orphan = path.with_name(f"{path.name}.99999.tmp")
    orphan.write_bytes(b"castecny nevalidni parquet")

    writer.write_minute("ES", "20260716", DAY, snapshot_rows(1, [7600.0]))

    assert not orphan.exists()  # uklizeno
    assert not list(path.parent.glob("*.tmp"))
    frame = pd.read_parquet(path)  # partice je vždy validní
    assert len(frame) == 4


def test_derived_dir_reserved_for_compute(tmp_path: Path) -> None:
    """Sanity: cesty partic odpovídají SPEC 5.1 rozvržení data adresáře."""
    settings = Settings(data_dir=tmp_path)
    assert settings.snapshots_dir == tmp_path / "snapshots"
    assert settings.ticks_dir == tmp_path / "ticks"
    assert settings.derived_dir == tmp_path / "derived"
