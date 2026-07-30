"""Testy sběrače kandidátů T6 (#256): trigger, ΔOI podpis, denní gating."""

import asyncio
import datetime as dt
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import create_engine

from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord
from gexlens_engine.storage.t6_store import T6Repository
from gexlens_engine.t6 import T6Collector, drop_trigger, put_oi_increase_below, read_daily_closes

TODAY = dt.date(2026, 7, 24)
MORNING = dt.datetime(2026, 7, 24, 13, 26, tzinfo=dt.UTC)


def test_drop_trigger_threshold() -> None:
    assert drop_trigger(7500.0, 7420.0, -1.0) is True  # −1,07 %
    assert drop_trigger(7500.0, 7450.0, -1.0) is False  # −0,67 %
    assert drop_trigger(0.0, 7400.0, -1.0) is False  # vadná data → žádný trigger


def test_put_oi_increase_below_counts_only_fresh_puts_under_spot() -> None:
    today = {
        (7300.0, "P"): 30_000.0,  # +25k pod spotem → počítá se
        (7350.0, "P"): 10_000.0,  # −2k (pokles) → ne
        (7500.0, "P"): 20_000.0,  # nad spotem → ne
        (7300.0, "C"): 50_000.0,  # call → ne
    }
    previous = {(7300.0, "P"): 5_000.0, (7350.0, "P"): 12_000.0}
    assert put_oi_increase_below(today, previous, spot=7400.0) == 25_000.0
    # Nový strike bez předchozího záznamu se počítá celý
    assert put_oi_increase_below({(7200.0, "P"): 1_000.0}, {}, spot=7400.0) == 1_000.0


def write_bars_day(data_dir: Path, symbol: str, day: dt.date, closes: list[float]) -> None:
    path = data_dir / "derived" / symbol / "bars" / f"{day.isoformat()}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"close": pa.array(closes, pa.float64())}), path)


def test_read_daily_closes_takes_last_two_sessions(tmp_path: Path) -> None:
    assert read_daily_closes(tmp_path, "ES", TODAY) is None
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 22), [7480.0, 7500.0])
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 23), [7490.0, 7420.0])
    write_bars_day(tmp_path, "ES", TODAY, [7430.0])  # dnešek se ignoruje
    closes = read_daily_closes(tmp_path, "ES", TODAY)
    assert closes is not None
    assert closes.last_day == dt.date(2026, 7, 23)
    assert closes.last_close == 7420.0
    assert closes.previous_close == 7500.0


class RecordingPublisher(PublisherLike):
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []

    async def status(self, **fields: object) -> None:
        return None

    async def publish(self, channel: str, data: dict[str, object]) -> None:
        self.messages.append((channel, data))


class FakeRuntime:
    expiry = "20260724"
    last_gex_levels = None
    last_flow = None
    last_profile = None


def make_collector(tmp_path: Path) -> tuple[T6Collector, T6Repository, RecordingPublisher]:
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 't6.sqlite'}")
    repository = T6Repository(db)
    repository.ensure_schema()
    oi_repository = OIEodRepository(db)
    oi_repository.ensure_schema()
    publisher = RecordingPublisher()
    collector = T6Collector(
        symbol="ES",
        repository=repository,
        oi_repository=oi_repository,
        publisher=publisher,
        data_dir=tmp_path,
    )
    return collector, repository, publisher


def test_collector_records_candidate_once_per_day(tmp_path: Path) -> None:
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 22), [7500.0])
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 23), [7400.0])  # −1,33 %
    collector, repository, publisher = make_collector(tmp_path)
    runtime = cast(EngineRuntime, FakeRuntime())

    # Před 13:25 UTC nic
    asyncio.run(collector.on_minute(MORNING.replace(hour=9), 7410.0, runtime))
    assert repository.list_for("ES") == []
    collector._evaluated_for = None  # gating spotřeboval dnešek — reset pro test

    asyncio.run(collector.on_minute(MORNING, 7412.0, runtime))
    rows = repository.list_for("ES")
    assert len(rows) == 1
    assert rows[0]["trigger_close_pct"] < -1.0
    assert rows[0]["overnight_move_pct"] is not None
    assert publisher.messages and publisher.messages[0][0] == "alerts"
    assert publisher.messages[0][1]["kind"] == "t6_candidate"

    # Druhá minuta téhož dne už nic nepřidá ani nepošle
    asyncio.run(collector.on_minute(MORNING + dt.timedelta(minutes=1), 7415.0, runtime))
    assert len(repository.list_for("ES")) == 1
    assert len(publisher.messages) == 1


def test_collector_quiet_without_trigger(tmp_path: Path) -> None:
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 22), [7500.0])
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 23), [7480.0])  # −0,27 %
    collector, repository, publisher = make_collector(tmp_path)
    asyncio.run(collector.on_minute(MORNING, 7480.0, cast(EngineRuntime, FakeRuntime())))
    assert repository.list_for("ES") == []
    assert publisher.messages == []


def test_collector_includes_put_mass_from_oi_archive(tmp_path: Path) -> None:
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 22), [7500.0])
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 23), [7400.0])
    collector, repository, _ = make_collector(tmp_path)
    collector.oi_repository.upsert_many(
        [
            OIRecord(
                symbol="ES",
                expiry="20260724",
                day=dt.date(2026, 7, 23),
                strike=7300.0,
                right="P",
                oi=5_000.0,
            ),  # noqa: E501
            OIRecord(
                symbol="ES", expiry="20260724", day=TODAY, strike=7300.0, right="P", oi=30_000.0
            ),  # noqa: E501
        ]
    )
    asyncio.run(collector.on_minute(MORNING, 7410.0, cast(EngineRuntime, FakeRuntime())))
    rows = repository.list_for("ES")
    assert rows[0]["put_oi_increase"] == 25_000.0
