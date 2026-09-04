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


def test_recent_sessions_sklada_obchodni_seance_pres_partice(tmp_path: Path) -> None:
    """#1001: seance = Globex obchodní den (večer D−1 + den D), ne UTC partice.

    Nedělní pahýl 22:00–23:59 patří pondělní seanci; pondělní večer 22:00+
    už úterní. Dřív se každá partice počítala jako seance → 20 „seancí" nikdy
    nedalo 20 vzorků na minutu.
    """
    data_dir = tmp_path / "data"

    def minutes(day: dt.date, start: dt.time, end: dt.time) -> list[tuple[dt.datetime, float]]:
        cur = dt.datetime.combine(day, start, tzinfo=dt.UTC)
        stop = dt.datetime.combine(day, end, tzinfo=dt.UTC)
        out = []
        while cur <= stop:
            out.append((cur, 1.0))
            cur += dt.timedelta(minutes=1)
        return out

    sunday, monday, tuesday = dt.date(2026, 8, 30), dt.date(2026, 8, 31), dt.date(2026, 9, 1)
    _write(data_dir, sunday, minutes(sunday, dt.time(22, 0), dt.time(23, 59)))
    _write(
        data_dir,
        monday,
        minutes(monday, dt.time(0, 0), dt.time(20, 59))
        + minutes(monday, dt.time(22, 0), dt.time(23, 59)),
    )
    _write(data_dir, tuesday, minutes(tuesday, dt.time(0, 0), dt.time(20, 59)))

    repo = BarsRepository(data_dir)
    sessions = repo.recent_sessions("ES", dt.date(2026, 9, 2), count=5)

    assert len(sessions) == 2  # pondělní a úterní seance, žádný nedělní „pahýl"
    monday_session, tuesday_session = sessions
    assert monday_session[0].ts == dt.datetime(2026, 8, 30, 22, 0, tzinfo=dt.UTC)
    assert monday_session[-1].ts == dt.datetime(2026, 8, 31, 20, 59, tzinfo=dt.UTC)
    assert len(monday_session) == 120 + 1260
    assert tuesday_session[0].ts == dt.datetime(2026, 8, 31, 22, 0, tzinfo=dt.UTC)
    assert len(tuesday_session) == 120 + 1260
    # count omezuje na poslední seance
    assert [s[0].ts for s in repo.recent_sessions("ES", dt.date(2026, 9, 2), count=1)] == [
        tuesday_session[0].ts
    ]
