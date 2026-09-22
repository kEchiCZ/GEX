"""Testy SnapshotWriteru (issue #11): schéma dle SPEC, čitelnost pandasem, atomický zápis."""

import datetime as dt
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.parquet_store import (
    SNAPSHOT_SCHEMA,
    PrintVolRow,
    SnapshotRow,
    SnapshotWriter,
)

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


def test_write_minute_upsert_same_minute_last_writer_wins(
    writer: SnapshotWriter, tmp_path: Path
) -> None:
    """#1047: minutu předání zapíše extended tasty větev i IBKR řetěz — jeden řádek, pozdější."""
    strikes = [7590.0, 7595.0]
    first = snapshot_rows(0, strikes)
    second = [
        SnapshotRow(**{**asdict(row), "volume": 7.0, "gamma": 0.02})
        for row in snapshot_rows(0, strikes)
    ]
    writer.write_minute("ES", "20260716", DAY, first)
    path = writer.write_minute("ES", "20260716", DAY, second)

    frame = pd.read_parquet(path)
    assert len(frame) == len(strikes) * 2
    assert not frame.duplicated(["ts_min", "strike", "right"]).any()
    assert set(frame["volume"]) == {7.0}
    assert set(frame["gamma"]) == {0.02}
    # pivot heatmapy (api/heatmap.py) na téhle partici nesmí spadnout
    frame[frame["right"] == "C"].pivot(index="ts_min", columns="strike", values="oi")


def test_existing_partition_with_duplicates_is_repaired_on_next_write(tmp_path: Path) -> None:
    """#1047: partice ze starého enginu s duplicitami se opraví prvním zápisem po nasazení."""
    strikes = [7590.0]
    rows = snapshot_rows(0, strikes)
    stale = [asdict(row) for row in rows] + [
        {**asdict(row), "volume": 7.0} for row in rows
    ]  # duplicita klíče; pozdější řádek nese volume IBKR řetězu
    path = tmp_path / "snapshots" / "ES" / "20260716" / "2026-07-16.parquet"
    path.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(stale, schema=SNAPSHOT_SCHEMA), path)

    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    writer.write_minute("ES", "20260716", DAY, snapshot_rows(1, strikes))

    frame = pd.read_parquet(path)
    assert not frame.duplicated(["ts_min", "strike", "right"]).any()
    minute_zero = frame[frame["ts_min"] == rows[0].ts_min]
    assert len(minute_zero) == 2
    assert set(minute_zero["volume"]) == {7.0}


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


def test_partition_buffer_slozeny_klic_presne_a_keep_existing(tmp_path: Path) -> None:
    """#1105 A: upsert nad Arrow tabulkou — shoda n-tice klíče je přesná (ne kartézský
    součin per sloupec) a `keep_existing` chrání změřený řádek před doplněným."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from gexlens_engine.storage.parquet_store import _PartitionBuffer

    schema = pa.schema([("ts", pa.int64()), ("strike", pa.float64()), ("v", pa.float64())])
    buffer = _PartitionBuffer(tmp_path / "p.parquet", schema)
    key = ("ts", "strike")
    buffer.append_and_write(
        [{"ts": 1, "strike": 10.0, "v": 1.0}, {"ts": 1, "strike": 20.0, "v": 2.0}], key
    )
    # (2, 10) a (1, 20) mají per-sloupcově společné hodnoty s (1, 10) — ta zůstat MUSÍ
    buffer.append_and_write(
        [{"ts": 2, "strike": 10.0, "v": 3.0}, {"ts": 1, "strike": 20.0, "v": 9.0}], key
    )
    rows = pq.read_table(tmp_path / "p.parquet").to_pylist()
    assert rows == [
        {"ts": 1, "strike": 10.0, "v": 1.0},
        {"ts": 1, "strike": 20.0, "v": 9.0},
        {"ts": 2, "strike": 10.0, "v": 3.0},
    ]
    # keep_existing: změřený (v > 0) řádek nepřepíše doplněný (v == 0)
    buffer.append_and_write(
        [{"ts": 1, "strike": 10.0, "v": 0.0}, {"ts": 3, "strike": 10.0, "v": 0.0}],
        key,
        keep_existing=lambda existing, incoming: incoming["v"] == 0.0 and existing["v"] != 0.0,
    )
    rows = pq.read_table(tmp_path / "p.parquet").to_pylist()
    assert rows[0] == {"ts": 1, "strike": 10.0, "v": 1.0}
    assert rows[-1] == {"ts": 3, "strike": 10.0, "v": 0.0}
    assert buffer.rows == 4
    # Restart uprostřed dne: partice s duplicitou se při načtení opraví (poslední vítězí)
    dup = pa.Table.from_pylist(
        [{"ts": 5, "strike": 1.0, "v": 1.0}, {"ts": 5, "strike": 1.0, "v": 2.0}], schema=schema
    )
    pq.write_table(dup, tmp_path / "q.parquet")
    fresh = _PartitionBuffer(tmp_path / "q.parquet", schema)
    fresh.append_and_write([{"ts": 6, "strike": 1.0, "v": 0.5}], key)
    assert pq.read_table(tmp_path / "q.parquet").to_pylist() == [
        {"ts": 5, "strike": 1.0, "v": 2.0},
        {"ts": 6, "strike": 1.0, "v": 0.5},
    ]


def test_measured_bar_closes_vynechava_doplnene(tmp_path: Path) -> None:
    """#1232: reference pro kontrolu backfillu = jen měřené minuty (živé / NULL zdroj)."""
    from gexlens_engine.ibkr.underlying import Bar

    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    day = dt.date(2026, 9, 15)
    t0 = dt.datetime(2026, 9, 15, 13, 30, tzinfo=dt.UTC)
    live = Bar(ts=t0, open=1, high=1, low=1, close=29100.0, volume=1, source=None)
    hist = Bar(
        ts=t0 + dt.timedelta(minutes=1),
        open=1,
        high=1,
        low=1,
        close=29530.0,
        volume=1,
        source="ibkr_hist",
    )
    writer.write_bars("NQ", day, [live, hist])
    assert writer.measured_bar_closes("NQ", day) == {t0: 29100.0}
    assert writer.measured_bar_closes("NQ", dt.date(2026, 9, 16)) == {}


def _bar_row(ts: dt.datetime, close: float) -> Bar:
    return Bar(ts=ts, open=close, high=close, low=close, close=close, volume=1.0, source=None)


def test_buffery_starych_dnu_se_uvolni_a_soubor_zustane(tmp_path: Path) -> None:
    """#1247: partice starší než dnes−1 se z paměti zahodí; na disku zůstává kompletní."""
    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    days = [dt.date(2026, 9, 20), dt.date(2026, 9, 21), dt.date(2026, 9, 22)]
    for index, day in enumerate(days):
        ts = dt.datetime.combine(day, dt.time(13, 30), tzinfo=dt.UTC)
        writer.write_bars("NQ", day, [_bar_row(ts, 30000.0 + index)])

    stats = writer.buffer_stats()
    assert stats["partitions"] == 2  # 21. a 22. 9.; 20. 9. uvolněno
    first = tmp_path / "derived" / "NQ" / "bars" / "2026-09-20.parquet"
    assert first.exists()
    assert len(pd.read_parquet(first)) == 1

    # Pozdní zápis do uvolněného dne si particii načte a řádek nepřepíše
    late = dt.datetime.combine(days[0], dt.time(14, 0), tzinfo=dt.UTC)
    writer.write_bars("NQ", days[0], [_bar_row(late, 29999.0)])
    reloaded = pd.read_parquet(first)
    assert len(reloaded) == 2
    assert set(reloaded["close"]) == {30000.0, 29999.0}


def test_strop_velikosti_evikuje_nejdele_nepouzity(tmp_path: Path) -> None:
    """#1247: druhá pojistka — nad stropem se zahazuje od nejdéle nepoužívaného."""
    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    writer.buffer_max_bytes = 1  # každý neprázdný buffer strop překročí
    day = dt.date(2026, 9, 22)
    ts = dt.datetime.combine(day, dt.time(13, 30), tzinfo=dt.UTC)
    for symbol in ("ES", "NQ", "QQQ"):
        writer.write_bars(symbol, day, [_bar_row(ts, 100.0)])
    # Zůstává jen poslední zapisovaná partice
    assert writer.buffer_stats()["partitions"] == 1
    for symbol in ("ES", "NQ", "QQQ"):
        path = tmp_path / "derived" / symbol / "bars" / f"{day.isoformat()}.parquet"
        assert len(pd.read_parquet(path)) == 1
