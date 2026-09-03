"""Testy opravy partic barů (#1002): bar patří do partice UTC dne svého ts."""

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.bar_partitions import plan_repartition
from gexlens_engine.storage.parquet_store import (
    BAR_SOURCE_LIVE,
    BAR_SOURCE_RECONSTRUCTED,
    SnapshotWriter,
    bar_partition_day,
)
from gexlens_engine.tasty.candles import CandleBar, partition_days

D = dt.date(2026, 9, 1)
D1 = dt.date(2026, 9, 2)


def row(ts: dt.datetime, volume: float, source: str | None = BAR_SOURCE_LIVE) -> dict[str, object]:
    return {
        "ts_min": ts,
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "volume": volume,
        "source": source,
    }


def test_bar_partition_day_je_utc_den_baru() -> None:
    assert bar_partition_day(dt.datetime(2026, 9, 1, 23, 59, tzinfo=dt.UTC)) == D
    assert bar_partition_day(dt.datetime(2026, 9, 2, 0, 0, tzinfo=dt.UTC)) == D1
    # naivní čas se bere jako UTC (tak ho vrací pyarrow bez tz)
    assert bar_partition_day(dt.datetime(2026, 9, 1, 23, 59)) == D


def test_partition_days_pokryje_obe_partice_seance() -> None:
    since = dt.datetime(2026, 9, 1, 22, 0, tzinfo=dt.UTC)
    until = dt.datetime(2026, 9, 2, 9, 30, tzinfo=dt.UTC)
    assert partition_days(since, until) == [D, D1]
    assert partition_days(until, until) == [D1]


def test_pulnocni_bar_vyhraje_finalni_z_cizi_partice() -> None:
    """Cyklus 00:00 zapsal finální 23:59 do D+1, v D zůstal provizorní — vyhrává finální v D."""
    ts = dt.datetime(2026, 9, 1, 23, 59, tzinfo=dt.UTC)
    plan = plan_repartition(
        {
            D: [row(ts, volume=30.0), row(ts - dt.timedelta(minutes=1), volume=10.0)],
            D1: [row(ts, volume=49.0), row(dt.datetime(2026, 9, 2, 0, 0, tzinfo=dt.UTC), 7.0)],
        }
    )
    assert plan.changed_days == [D, D1]
    assert [r["volume"] for r in plan.rows_by_day[D]] == [10.0, 49.0]
    assert [r["ts_min"] for r in plan.rows_by_day[D1]] == [
        dt.datetime(2026, 9, 2, 0, 0, tzinfo=dt.UTC)
    ]
    assert (plan.moved, plan.replaced, plan.dropped) == (1, 1, 0)


def test_rekonstrukce_v_cizi_partici_prohraje_s_merenym_barem() -> None:
    """Blok 22:00–23:59 D−1 jako tasty_candle v D, v D−1 měřené bary — rekonstrukce se zahodí."""
    evening = [dt.datetime(2026, 9, 1, 22, m, tzinfo=dt.UTC) for m in range(3)]
    plan = plan_repartition(
        {
            D: [row(ts, volume=100.0) for ts in evening],
            D1: [row(ts, volume=500.0, source=BAR_SOURCE_RECONSTRUCTED) for ts in evening]
            + [row(dt.datetime(2026, 9, 2, 8, 0, tzinfo=dt.UTC), 3.0)],
        }
    )
    assert [r["volume"] for r in plan.rows_by_day[D]] == [100.0, 100.0, 100.0]
    assert len(plan.rows_by_day[D1]) == 1
    assert (plan.moved, plan.replaced, plan.dropped) == (0, 0, 3)


def test_bar_bez_protejsku_se_presune_a_netknute_dny_se_nemeni() -> None:
    lonely = dt.datetime(2026, 9, 1, 22, 5, tzinfo=dt.UTC)
    other_day = dt.date(2026, 8, 20)
    plan = plan_repartition(
        {
            D1: [row(lonely, 8.0, source=BAR_SOURCE_RECONSTRUCTED)],
            other_day: [row(dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.UTC), 1.0)],
        }
    )
    assert plan.changed_days == [D, D1]  # D vznikne nově, D1 se vyprázdní
    assert plan.rows_by_day[D][0]["ts_min"] == lonely
    assert plan.rows_by_day[D1] == []
    assert other_day not in plan.rows_by_day
    assert (plan.moved, plan.replaced, plan.dropped) == (1, 0, 0)


def test_spravne_rozlozene_partice_nemaji_co_opravovat() -> None:
    plan = plan_repartition({D: [row(dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.UTC), 1.0)]})
    assert plan.changed_days == []


@pytest.fixture
def writer(tmp_path: Path) -> SnapshotWriter:
    return SnapshotWriter(Settings(data_dir=tmp_path))


def bar(ts: dt.datetime, volume: float) -> Bar:
    return Bar(ts=ts, open=1.0, high=2.0, low=0.5, close=1.5, volume=volume)


def candle(ts: dt.datetime, volume: float) -> CandleBar:
    return CandleBar(ts=ts, open=1.0, high=2.0, low=0.5, close=1.5, volume=volume)


def test_write_bars_by_day_rozdeli_pulnoc_do_dvou_partic(writer: SnapshotWriter) -> None:
    """Runtime předá finální 23:59 a provizorní 00:00 v jednom cyklu — každý do své partice."""
    last = bar(dt.datetime(2026, 9, 1, 23, 59, tzinfo=dt.UTC), 49.0)
    first = bar(dt.datetime(2026, 9, 2, 0, 0, tzinfo=dt.UTC), 7.0)
    paths = writer.write_bars_by_day("ES", [last, first])

    assert [p.name for p in paths] == ["2026-09-01.parquet", "2026-09-02.parquet"]
    assert list(pd.read_parquet(paths[0])["volume"]) == [49.0]
    assert list(pd.read_parquet(paths[1])["volume"]) == [7.0]


def test_bar_minutes_for_days_sjednoti_partice(writer: SnapshotWriter) -> None:
    evening = candle(dt.datetime(2026, 9, 1, 22, 0, tzinfo=dt.UTC), 1.0)
    morning = bar(dt.datetime(2026, 9, 2, 8, 0, tzinfo=dt.UTC), 1.0)
    writer.write_bars_by_day("ES", [evening, morning])

    assert writer.bar_minutes_for_days("ES", [D, D1]) == {evening.ts, morning.ts}
    assert writer.bar_minutes_for_days("ES", [D1]) == {morning.ts}
