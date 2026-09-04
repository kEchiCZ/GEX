"""Testy SnapshotWriteru (issue #11): schéma dle SPEC, čitelnost pandasem, atomický zápis."""

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from gexlens_engine.config import Settings
from gexlens_engine.storage.parquet_store import PrintVolRow, SnapshotRow, SnapshotWriter

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


def test_derived_dir_reserved_for_compute(tmp_path: Path) -> None:
    """Sanity: cesty partic odpovídají SPEC 5.1 rozvržení data adresáře."""
    settings = Settings(data_dir=tmp_path)
    assert settings.snapshots_dir == tmp_path / "snapshots"
    assert settings.derived_dir == tmp_path / "derived"


def test_read_last_cum_delta_okno_seance(tmp_path: Path) -> None:
    """#638: seed CumΔ čte jen řádky v okně seance, přes dvě UTC partice."""
    import datetime as dt

    from gexlens_engine.compute.cumdelta import FlowRow
    from gexlens_engine.storage.parquet_store import read_last_cum_delta

    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    sunday = dt.date(2026, 7, 19)
    monday = dt.date(2026, 7, 20)
    # Neděle: řádek PŘED openem (mimo okno) + večer po openu (v okně)
    writer.write_flow(
        "ES",
        sunday,
        [
            FlowRow(dt.datetime(2026, 7, 19, 21, 30, tzinfo=dt.UTC), 5.0, 999.0),
            FlowRow(dt.datetime(2026, 7, 19, 23, 0, tzinfo=dt.UTC), 5.0, 5.0),
        ],
    )
    writer.write_flow(
        "ES",
        monday,
        [
            FlowRow(dt.datetime(2026, 7, 20, 15, 0, tzinfo=dt.UTC), 3.0, 8.0),
            FlowRow(dt.datetime(2026, 7, 20, 22, 30, tzinfo=dt.UTC), 4.0, 12.0),  # už úterní seance
        ],
    )
    paths = [
        tmp_path / "derived" / "ES" / "flow" / f"{day.isoformat()}.parquet"
        for day in (sunday, monday)
    ]
    start = dt.datetime(2026, 7, 19, 22, 0, tzinfo=dt.UTC)
    end = dt.datetime(2026, 7, 20, 22, 0, tzinfo=dt.UTC)
    assert read_last_cum_delta(paths, start=start, end=end) == 8.0
    # Jen večerní část (restart v neděli v noci)
    assert read_last_cum_delta(paths[:1], start=start, end=end) == 5.0
    # Řádek 21:30 (před openem) patří PŘEDCHOZÍ seanci — do pondělního okna nesmí
    prev_start = dt.datetime(2026, 7, 18, 22, 0, tzinfo=dt.UTC)
    assert read_last_cum_delta(paths, start=prev_start, end=start) == 999.0
    # Okno bez jediného řádku → None
    empty_end = dt.datetime(2026, 7, 18, 22, 0, tzinfo=dt.UTC)
    assert (
        read_last_cum_delta(
            paths, start=dt.datetime(2026, 7, 17, 22, 0, tzinfo=dt.UTC), end=empty_end
        )
        is None
    )  # noqa: E501


def test_printvol_partition_roundtrip(writer: SnapshotWriter, tmp_path: Path) -> None:
    """#1007: řada printvol per expirace, NULL zůstává NULL (ne 0)."""
    ts = dt.datetime(2026, 9, 4, 14, 0, tzinfo=dt.UTC)
    rows = [
        PrintVolRow(
            ts_min=ts, strike=7600.0, right="C", volume_delta=15.0, printed=7.0, structured=8.0
        ),
        PrintVolRow(
            ts_min=ts, strike=7600.0, right="P", volume_delta=4.0, printed=None, structured=None
        ),
    ]
    assert writer.write_printvol("ES", "20260904", DAY, []) is None  # prázdno se nezapisuje
    path = writer.write_printvol("ES", "20260904", DAY, rows)

    assert (
        path == tmp_path / "derived" / "ES" / "20260904" / "printvol" / f"{DAY.isoformat()}.parquet"
    )
    frame = pd.read_parquet(path)
    assert list(frame.columns) == [
        "ts_min",
        "strike",
        "right",
        "volume_delta",
        "printed",
        "structured",
    ]
    assert list(frame["printed"].isna()) == [False, True]
    assert float(frame["structured"].iloc[0]) == 8.0
