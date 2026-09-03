"""#1002: load_range vrátí jednu minutu jednou, i když ji nesou dvě partice."""

import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from gexlens_news.bars import BarsRepository


def _write(data_dir: Path, day: dt.date, rows: list[tuple[dt.datetime, float]]) -> None:
    directory = data_dir / "derived" / "ES" / "bars"
    directory.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        [
            {"ts_min": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": volume}
            for ts, volume in rows
        ]
    )
    pq.write_table(table, directory / f"{day.isoformat()}.parquet")


def test_load_range_deduplikuje_minutu_a_preferuje_domaci_partici(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    midnight_bar = dt.datetime(2026, 9, 1, 23, 59, tzinfo=dt.UTC)
    evening = dt.datetime(2026, 9, 1, 22, 0, tzinfo=dt.UTC)
    # D: měřené bary vč. 23:59 (objem 49); D+1: tatáž 23:59 podruhé (starý půlnoční
    # zápis, objem 30) + rekonstruovaný 22:00 (objem 999) + vlastní 00:00
    _write(data_dir, dt.date(2026, 9, 1), [(evening, 100.0), (midnight_bar, 49.0)])
    _write(
        data_dir,
        dt.date(2026, 9, 2),
        [
            (midnight_bar, 30.0),
            (evening, 999.0),
            (dt.datetime(2026, 9, 2, 0, 0, tzinfo=dt.UTC), 7.0),
        ],
    )

    bars = BarsRepository(data_dir).load_range(
        "ES",
        dt.datetime(2026, 9, 1, 21, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 9, 2, 1, 0, tzinfo=dt.UTC),
    )

    assert [bar.ts for bar in bars] == [
        evening,
        midnight_bar,
        dt.datetime(2026, 9, 2, 0, 0, tzinfo=dt.UTC),
    ]
    # vyhrála domácí partice (UTC den baru), ne pořadí čtení
    assert [bar.volume for bar in bars] == [100.0, 49.0, 7.0]
