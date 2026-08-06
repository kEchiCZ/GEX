"""Testy sběrače kandidátů T6 (#256): trigger, ΔOI podpis, denní gating."""

import asyncio
import datetime as dt
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import create_engine, text

from gexlens_engine.runtime import EngineRuntime, PublisherLike
from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord
from gexlens_engine.storage.t6_store import T6_CONVENTION_VERSION, T6Repository
from gexlens_engine.t6 import (
    T6Collector,
    drop_trigger,
    put_oi_increase_below,
    read_daily_closes,
    recompute_stale_candidates,
)

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


def write_bars_day(
    data_dir: Path, symbol: str, day: dt.date, bars: list[tuple[dt.time, float]]
) -> None:
    """Zapíše denní partici barů: (čas UTC, close) — settle konvence potřebuje ts_min."""
    path = data_dir / "derived" / symbol / "bars" / f"{day.isoformat()}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = [dt.datetime.combine(day, time, tzinfo=dt.UTC) for time, _ in bars]
    pq.write_table(
        pa.table(
            {
                "ts_min": pa.array(ts, pa.timestamp("us", tz="UTC")),
                "close": pa.array([close for _, close in bars], pa.float64()),
            }
        ),
        path,
    )


def pre_settle(closes: list[float]) -> list[tuple[dt.time, float]]:
    """Bary před settle hranicí (poslední v 19:59 UTC) — zkratka pro testy."""
    count = len(closes)
    return [(dt.time(19, 60 - count + i), close) for i, close in enumerate(closes)]


def test_read_daily_closes_takes_last_two_sessions(tmp_path: Path) -> None:
    assert read_daily_closes(tmp_path, "ES", TODAY) is None
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 22), pre_settle([7480.0, 7500.0]))
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 23), pre_settle([7490.0, 7420.0]))
    write_bars_day(tmp_path, "ES", TODAY, pre_settle([7430.0]))  # dnešek se ignoruje
    closes = read_daily_closes(tmp_path, "ES", TODAY)
    assert closes is not None
    assert closes.last_day == dt.date(2026, 7, 23)
    assert closes.last_close == 7420.0
    assert closes.previous_close == 7500.0


def test_read_daily_closes_cuts_day_at_settle(tmp_path: Path) -> None:
    """Close dne = poslední bar PŘED 20:00 UTC; bar ve 20:01 už patří další seanci (#498)."""
    write_bars_day(
        tmp_path,
        "ES",
        dt.date(2026, 7, 22),
        [(dt.time(19, 59), 7500.0), (dt.time(20, 1), 7550.0)],
    )
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 23), [(dt.time(19, 59), 7420.0)])
    closes = read_daily_closes(tmp_path, "ES", TODAY)
    assert closes is not None
    assert closes.previous_close == 7500.0  # ne 7550 z baru po settle
    assert closes.last_close == 7420.0


def test_read_daily_closes_skips_day_without_pre_settle_bars(tmp_path: Path) -> None:
    """Den jen s bary po settle (neděle — Globex otevírá 22:00 UTC) se přeskočí."""
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 21), pre_settle([7480.0]))
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 22), pre_settle([7500.0]))
    write_bars_day(
        tmp_path,
        "ES",
        dt.date(2026, 7, 23),
        [(dt.time(22, 5), 7460.0), (dt.time(23, 59), 7455.0)],
    )
    closes = read_daily_closes(tmp_path, "ES", TODAY)
    assert closes is not None
    assert closes.last_day == dt.date(2026, 7, 22)
    assert closes.last_close == 7500.0
    assert closes.previous_close == 7480.0


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
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 22), pre_settle([7500.0]))
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 23), pre_settle([7400.0]))  # −1,33 %
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
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 22), pre_settle([7500.0]))
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 23), pre_settle([7480.0]))  # −0,27 %
    collector, repository, publisher = make_collector(tmp_path)
    asyncio.run(collector.on_minute(MORNING, 7480.0, cast(EngineRuntime, FakeRuntime())))
    assert repository.list_for("ES") == []
    assert publisher.messages == []


def test_collector_includes_put_mass_from_oi_archive(tmp_path: Path) -> None:
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 22), pre_settle([7500.0]))
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 23), pre_settle([7400.0]))
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


def test_overnight_move_measured_from_settle_close(tmp_path: Path) -> None:
    """Gap settle→premarket: bar po settle nesmí posunout referenční close (#498)."""
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 22), pre_settle([7500.0]))
    write_bars_day(
        tmp_path,
        "ES",
        dt.date(2026, 7, 23),
        [(dt.time(19, 59), 7400.0), (dt.time(20, 30), 7350.0)],  # po settle → další seance
    )
    collector, repository, _ = make_collector(tmp_path)
    asyncio.run(collector.on_minute(MORNING, 7437.0, cast(EngineRuntime, FakeRuntime())))
    rows = repository.list_for("ES")
    assert len(rows) == 1
    assert rows[0]["trigger_close_pct"] == (7400.0 / 7500.0 - 1) * 100  # −1,33 % ze settle closů
    assert rows[0]["overnight_move_pct"] == (7437.0 / 7400.0 - 1) * 100  # +0,5 % od settle
    assert rows[0]["convention_version"] == T6_CONVENTION_VERSION


def seed_old_candidate(
    repository: T6Repository,
    *,
    symbol: str = "ES",
    day: dt.date = TODAY,
    trigger_close_pct: float = -1.5,
    overnight_move_pct: float | None = 0.3,
    spot: float = 7437.0,
) -> None:
    """Zapíše kandidáta a degraduje ho na verzi 1 (stav před #498)."""
    repository.upsert(
        symbol=symbol,
        day=day,
        trigger_close_pct=trigger_close_pct,
        overnight_move_pct=overnight_move_pct,
        put_oi_increase=12_000.0,
        gex_regime="negative",
        max_pain=7450.0,
        spot=spot,
        evaluated_at=MORNING,
    )
    with repository._engine.begin() as conn:
        conn.execute(
            text("UPDATE t6_occurrences SET convention_version = 1 WHERE day = :day"),
            {"day": day.isoformat()},
        )


def test_recompute_updates_old_candidates_to_settle_convention(tmp_path: Path) -> None:
    _, repository, _ = make_collector(tmp_path)
    # Stará konvence viděla poslední bar UTC dne (20:30 → 7350); settle vidí 19:59 → 7400
    write_bars_day(tmp_path, "ES", dt.date(2026, 7, 22), pre_settle([7500.0]))
    write_bars_day(
        tmp_path,
        "ES",
        dt.date(2026, 7, 23),
        [(dt.time(19, 59), 7400.0), (dt.time(20, 30), 7350.0)],
    )
    seed_old_candidate(
        repository, trigger_close_pct=(7350.0 / 7500.0 - 1) * 100, overnight_move_pct=1.18
    )

    updated = recompute_stale_candidates(repository, tmp_path)

    assert updated == 1
    rows = repository.list_for("ES")
    assert len(rows) == 1
    assert rows[0]["trigger_close_pct"] == (7400.0 / 7500.0 - 1) * 100
    assert rows[0]["overnight_move_pct"] == (7437.0 / 7400.0 - 1) * 100
    assert rows[0]["put_oi_increase"] == 12_000.0  # metriky nezávislé na řezu dne zůstávají
    assert rows[0]["convention_version"] == T6_CONVENTION_VERSION
    # Druhý běh je no-op — nic staršího nezbylo
    assert recompute_stale_candidates(repository, tmp_path) == 0


def test_recompute_drops_candidate_without_bars(tmp_path: Path) -> None:
    _, repository, _ = make_collector(tmp_path)
    seed_old_candidate(repository)  # žádné bary v archivu

    assert recompute_stale_candidates(repository, tmp_path) == 0
    assert repository.list_for("ES") == []


def test_recompute_leaves_current_convention_untouched(tmp_path: Path) -> None:
    _, repository, _ = make_collector(tmp_path)
    repository.upsert(
        symbol="ES",
        day=TODAY,
        trigger_close_pct=-1.2,
        overnight_move_pct=0.4,
        put_oi_increase=None,
        gex_regime=None,
        max_pain=None,
        spot=7400.0,
        evaluated_at=MORNING,
    )  # bez barů v archivu — kdyby se řádek bral jako starý, smazal by se

    assert recompute_stale_candidates(repository, tmp_path) == 0
    rows = repository.list_for("ES")
    assert len(rows) == 1
    assert rows[0]["trigger_close_pct"] == -1.2


def test_ensure_schema_adds_convention_version_to_old_table(tmp_path: Path) -> None:
    """Tabulka založená před #498 dostane sloupec a staré řádky verzi 1."""
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'old.sqlite'}")
    with db.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE t6_occurrences ("
                "day DATE NOT NULL, symbol VARCHAR(16) NOT NULL, "
                "trigger_close_pct FLOAT NOT NULL, overnight_move_pct FLOAT, "
                "put_oi_increase FLOAT, gex_regime VARCHAR(16), max_pain FLOAT, "
                "spot FLOAT NOT NULL, evaluated_at TIMESTAMP NOT NULL, "
                "PRIMARY KEY (day, symbol))"
            )
        )
        conn.execute(
            text(
                "INSERT INTO t6_occurrences (day, symbol, trigger_close_pct, spot, evaluated_at) "
                "VALUES ('2026-07-24', 'ES', -1.4, 7400.0, '2026-07-24 13:26:00')"
            )
        )
    repository = T6Repository(db)
    repository.ensure_schema()
    rows = repository.list_for("ES")
    assert rows[0]["convention_version"] == 1
    assert len(repository.list_stale(T6_CONVENTION_VERSION)) == 1
