"""Testy SnapshotWriteru (issue #11): schéma dle SPEC, čitelnost pandasem, atomický zápis."""

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.underlying import Bar
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


def test_bars_upsert_by_minute(writer: SnapshotWriter) -> None:
    """ADR-0005: provizorní bar minuty nahradí finální, nezdvojí se."""
    ts = dt.datetime(2026, 7, 16, 15, 0, tzinfo=dt.UTC)
    provisional = Bar(ts=ts, open=100.0, high=102.0, low=99.0, close=101.0, volume=300.0)
    path = writer.write_bars("ES", DAY, [provisional])
    assert len(pd.read_parquet(path)) == 1

    final = Bar(ts=ts, open=100.0, high=105.0, low=98.0, close=104.0, volume=1200.0)
    later = Bar(
        ts=ts + dt.timedelta(minutes=1), open=104.0, high=106.0, low=103.0,
        close=105.0, volume=200.0,
    )  # fmt: skip
    writer.write_bars("ES", DAY, [final])
    path = writer.write_bars("ES", DAY, [later])

    frame = pd.read_parquet(path).sort_values("ts_min")
    assert len(frame) == 2  # jedna minuta = jeden řádek
    assert list(frame["close"]) == [104.0, 105.0]
    assert list(frame["volume"]) == [1200.0, 200.0]


def test_new_writer_continues_existing_partition(tmp_path: Path) -> None:
    """Restart enginu uprostřed dne: nový writer naváže na existující partici."""
    settings = Settings(data_dir=tmp_path)
    path = SnapshotWriter(settings).write_minute("ES", "20260716", DAY, snapshot_rows(0, [7600.0]))

    SnapshotWriter(settings).write_minute("ES", "20260716", DAY, snapshot_rows(1, [7600.0]))

    frame = pd.read_parquet(path)
    assert len(frame) == 4  # obě minuty, žádná ztráta po "restartu"


def test_restart_mid_day_replaces_provisional_bar(tmp_path: Path) -> None:
    """ADR-0005 + restart: nový writer po pádu nahradí provizorní bar finálním (#157).

    Upsert podle ts_min musí fungovat i přes hranici procesu — `_ensure_loaded`
    načte partici včetně provizorního řádku a finální bar ho nahradí, nezdvojí.
    """
    settings = Settings(data_dir=tmp_path)
    ts = dt.datetime(2026, 7, 16, 15, 0, tzinfo=dt.UTC)

    # Proces 1: finální bar 14:59 + provizorní 15:00, pak "spadne"
    writer_before = SnapshotWriter(settings)
    earlier = Bar(
        ts=ts - dt.timedelta(minutes=1), open=99.0, high=100.0, low=98.0, close=99.5, volume=500.0
    )
    provisional = Bar(ts=ts, open=100.0, high=102.0, low=99.0, close=101.0, volume=300.0)
    writer_before.write_bars("ES", DAY, [earlier])
    path = writer_before.write_bars("ES", DAY, [provisional])
    assert len(pd.read_parquet(path)) == 2

    # Proces 2 (restart): finální bar téže minuty nahradí provizorní řádek
    final = Bar(ts=ts, open=100.0, high=105.0, low=98.0, close=104.0, volume=1200.0)
    path = SnapshotWriter(settings).write_bars("ES", DAY, [final])

    frame = pd.read_parquet(path).sort_values("ts_min")
    assert len(frame) == 2  # jedna minuta = jeden řádek i přes restart
    assert list(frame["close"]) == [99.5, 104.0]
    assert list(frame["volume"]) == [500.0, 1200.0]


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
