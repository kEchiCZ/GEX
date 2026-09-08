"""Testy opravy partic barů (#1002): bar patří do partice UTC dne svého ts."""

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.bar_partitions import bar_rank, plan_repartition
from gexlens_engine.storage.parquet_store import (
    BAR_SOURCE_HISTORICAL,
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


def hist(ts: dt.datetime, volume: float) -> Bar:
    return Bar(
        ts=ts, open=1.0, high=2.0, low=0.5, close=1.5, volume=volume, source=BAR_SOURCE_HISTORICAL
    )


def _sources(writer: SnapshotWriter, day: dt.date) -> dict[dt.datetime, tuple[str, float]]:
    frame = pd.read_parquet(
        writer._settings.derived_dir / "ES" / "bars" / f"{day.isoformat()}.parquet"
    )
    return {
        ts.to_pydatetime().replace(tzinfo=dt.UTC): (src, float(vol))
        for ts, src, vol in zip(frame["ts_min"], frame["source"], frame["volume"], strict=True)
    }


def test_backfill_z_ibkr_historical_neprepise_zmerenou_minutu(writer: SnapshotWriter) -> None:
    """#1055 AC2: živý zápis historickou hodnotu nahradí, obráceně ne."""
    t0 = dt.datetime(2026, 9, 1, 14, 0, tzinfo=dt.UTC)
    t1 = t0 + dt.timedelta(minutes=1)
    t2 = t0 + dt.timedelta(minutes=2)
    # Živě změřeno t0; t2 rekonstruováno z dxFeed
    writer.write_bars("ES", D, [bar(t0, 10.0), candle(t2, 1.0)])
    # Backfill přinese všechny tři minuty
    writer.write_bars("ES", D, [hist(t0, 99.0), hist(t1, 5.0), hist(t2, 7.0)])
    rows = _sources(writer, D)
    assert rows[t0] == (BAR_SOURCE_LIVE, 10.0)  # změřená minuta zůstala
    assert rows[t1] == (BAR_SOURCE_HISTORICAL, 5.0)  # díra se doplnila
    assert rows[t2] == (BAR_SOURCE_HISTORICAL, 7.0)  # historical přebil rekonstrukci
    # Živý zápis téže minuty historickou hodnotu nahradí
    writer.write_bars("ES", D, [bar(t1, 6.0)])
    assert _sources(writer, D)[t1] == (BAR_SOURCE_LIVE, 6.0)
    # Provizorní → finální bar téže minuty (ADR-0005) se dál nahrazuje
    writer.write_bars("ES", D, [bar(t1, 8.0)])
    assert _sources(writer, D)[t1] == (BAR_SOURCE_LIVE, 8.0)


def test_bar_rank_puvod_pred_objemem() -> None:
    """Plán opravy partic (#1002) řadí měřený > ibkr_hist > tasty_candle, teprve pak objem."""
    ts = dt.datetime(2026, 9, 1, 14, 0, tzinfo=dt.UTC)
    live = row(ts, 1.0)
    old_null = row(ts, 1.0, source=None)
    historical = row(ts, 100.0, source=BAR_SOURCE_HISTORICAL)
    reconstructed = row(ts, 1000.0, source=BAR_SOURCE_RECONSTRUCTED)
    assert bar_rank(live, D) > bar_rank(historical, D) > bar_rank(reconstructed, D)
    assert bar_rank(old_null, D)[0] == bar_rank(live, D)[0]
