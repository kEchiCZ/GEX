"""Testy recorderu surových TimeAndSale printů (#795)."""

import datetime as dt
from pathlib import Path

import pyarrow.parquet as pq

from gexlens_engine.config import Settings
from gexlens_engine.storage.parquet_store import SnapshotWriter, TastyTradeRow
from gexlens_engine.tasty.trades_recorder import TradesRecorder

TS_MS = 1_787_176_000_000  # 2026-08-19T21:46:40Z
FIXED_NOW = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.UTC)


def event(symbol: str, *, time_ms: object = TS_MS, price: object = 12.5) -> list[object]:
    """TimeAndSale values dle EVENT_FIELDS pořadí."""
    return [symbol, time_ms, price, 3.0, "BUY", False, "true"]


def make_recorder() -> TradesRecorder:
    recorder = TradesRecorder(clock=lambda: FIXED_NOW)
    recorder.set_mapping({"./ESU26C7600:XCME": "ES", "./NQU26P23000:XCME": "NQ"})
    return recorder


def test_records_mapped_trade_with_event_fields() -> None:
    recorder = make_recorder()

    recorder.on_event("TimeAndSale", event("./ESU26C7600:XCME"))

    batches = recorder.drain()
    (root, day), rows = next(iter(batches.items()))
    assert root == "ES"
    assert day == dt.date(2026, 8, 19)  # den z časové značky eventu, ne z flushe
    row = rows[0]
    assert row.price == 12.5
    assert row.size == 3.0
    assert row.aggressor == "BUY"
    assert row.spread_leg is False
    assert row.eth is True  # string „true" z COMPACT formátu
    assert recorder.recorded == 1


def test_unmapped_symbol_is_dropped_and_counted() -> None:
    """Podklad ani symboly před první chain mapou se nezaznamenávají (záměr)."""
    recorder = make_recorder()

    recorder.on_event("TimeAndSale", event("/ESU26:XCME"))  # front future není v mapě

    assert recorder.drain() == {}
    assert recorder.dropped_unmapped == 1
    assert recorder.recorded == 0


def test_other_events_and_priceless_prints_ignored() -> None:
    recorder = make_recorder()

    recorder.on_event("Quote", ["./ESU26C7600:XCME", 1.0, 2.0, 3, 4])
    recorder.on_event("TimeAndSale", event("./ESU26C7600:XCME", price="NaN"))

    assert recorder.drain() == {}


def test_missing_event_time_falls_back_to_clock() -> None:
    recorder = make_recorder()

    recorder.on_event("TimeAndSale", event("./NQU26P23000:XCME", time_ms=None))

    batches = recorder.drain()
    ((root, day),) = batches.keys()
    assert root == "NQ"
    assert day == FIXED_NOW.date()


def test_drain_groups_by_root_and_day_and_clears() -> None:
    recorder = make_recorder()
    recorder.on_event("TimeAndSale", event("./ESU26C7600:XCME"))
    recorder.on_event("TimeAndSale", event("./ESU26C7600:XCME", time_ms=TS_MS + 86_400_000))
    recorder.on_event("TimeAndSale", event("./NQU26P23000:XCME"))

    batches = recorder.drain()

    assert set(batches) == {
        ("ES", dt.date(2026, 8, 19)),
        ("ES", dt.date(2026, 8, 20)),
        ("NQ", dt.date(2026, 8, 19)),
    }
    assert recorder.drain() == {}  # druhý drain je prázdný


def test_writer_appends_to_daily_partition(tmp_path: Path) -> None:
    """Roundtrip přes SnapshotWriter: flushe se do partice PŘIDÁVAJÍ."""
    settings = Settings(data_dir=tmp_path)
    writer = SnapshotWriter(settings)
    day = dt.date(2026, 8, 19)
    ts = dt.datetime(2026, 8, 19, 21, 46, 40, tzinfo=dt.UTC)
    row = TastyTradeRow(
        ts=ts,
        streamer_symbol="./ESU26C7600:XCME",
        price=12.5,
        size=3.0,
        aggressor="BUY",
        spread_leg=False,
        eth=None,
    )

    path = writer.write_tasty_trades("ES", day, [row])
    writer.write_tasty_trades("ES", day, [row])

    assert path == settings.trades_dir / "ES" / "2026-08-19.parquet"
    table = pq.read_table(path)
    assert table.num_rows == 2
    record = table.to_pylist()[0]
    assert record["aggressor"] == "BUY"
    assert record["eth"] is None
    assert record["ts"] == ts
