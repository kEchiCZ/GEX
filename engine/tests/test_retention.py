"""Testy RetentionJobu (issue #12): purge >14 dní, oi_eod netknutá, disk limit."""

import datetime as dt
from pathlib import Path

from sqlalchemy import create_engine

from gexlens_engine.config import Settings
from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord
from gexlens_engine.storage.retention import RetentionJob

TODAY = dt.date(2026, 7, 16)
DAY_15_OLD = TODAY - dt.timedelta(days=15)
DAY_13_OLD = TODAY - dt.timedelta(days=13)


def make_partition(root: Path, *parts: str, day: dt.date) -> Path:
    directory = root.joinpath(*parts)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day.isoformat()}.parquet"
    path.write_bytes(b"x" * 1024)
    return path


def test_purge_deletes_15_days_keeps_13_days(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    old_snap = make_partition(settings.snapshots_dir, "ES", "20260716", day=DAY_15_OLD)
    new_snap = make_partition(settings.snapshots_dir, "ES", "20260716", day=DAY_13_OLD)
    old_ticks = make_partition(settings.ticks_dir, "ES", day=DAY_15_OLD)
    new_ticks = make_partition(settings.ticks_dir, "ES", day=DAY_13_OLD)
    old_derived = make_partition(settings.derived_dir, "ES", "20260716", day=DAY_15_OLD)

    report = RetentionJob(settings).purge(TODAY)

    # AC: partice 15 dní stará smazána, 13 dní ponechána
    assert not old_snap.exists()
    assert not old_ticks.exists()
    assert not old_derived.exists()
    assert new_snap.exists()
    assert new_ticks.exists()
    assert set(report.deleted) == {old_snap, old_ticks, old_derived}
    assert report.kept_files == 2


def test_boundary_day_exactly_retention_is_kept(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    boundary = make_partition(
        settings.snapshots_dir, "ES", "20260716", day=TODAY - dt.timedelta(days=14)
    )

    RetentionJob(settings).purge(TODAY)

    assert boundary.exists()  # „starší než 14 dní" — přesně 14 dní se ještě drží


def test_oi_eod_never_touched_by_purge(tmp_path: Path) -> None:
    """AC + R4: purge job se oi_eod nedotkne, ani když jsou data starší než retence."""
    settings = Settings(data_dir=tmp_path / "data")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'gexlens.db'}")
    repository = OIEodRepository(engine)
    repository.ensure_schema()
    ancient = TODAY - dt.timedelta(days=400)
    repository.upsert_many(
        [
            OIRecord("ES", "20250601", 7000.0, "C", ancient, 1234.0),
            OIRecord("ES", "20260716", 7600.0, "P", DAY_15_OLD, 555.0),
        ]
    )
    make_partition(settings.snapshots_dir, "ES", "20260716", day=DAY_15_OLD)

    RetentionJob(settings).purge(TODAY)

    assert repository.days("ES") == [ancient, DAY_15_OLD]  # archiv beze změny
    assert repository.get_oi("ES", ancient, 7000.0, "C") == 1234.0


def test_unparseable_partition_name_is_kept(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    directory = settings.snapshots_dir / "ES" / "20260716"
    directory.mkdir(parents=True)
    weird = directory / "not-a-date.parquet"
    weird.write_bytes(b"x")

    report = RetentionJob(settings).purge(TODAY)

    assert weird.exists()
    assert report.kept_files == 1
    assert report.deleted == ()


def test_empty_partition_dirs_removed(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    old = make_partition(settings.snapshots_dir, "ES", "20260101", day=DAY_15_OLD)

    RetentionJob(settings).purge(TODAY)

    assert not old.parent.exists()  # prázdný adresář expirace uklizen
    assert settings.snapshots_dir.exists()  # kořeny zůstávají


def test_disk_usage_and_limit_alert(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, disk_limit_gb=1.0)
    make_partition(settings.snapshots_dir, "ES", "20260716", day=DAY_13_OLD)

    report = RetentionJob(settings).purge(TODAY)
    assert report.disk_usage_bytes >= 1024
    assert not report.disk_limit_exceeded

    tiny_limit = Settings(data_dir=tmp_path, disk_limit_gb=1e-9)
    report_exceeded = RetentionJob(tiny_limit).purge(TODAY)
    assert report_exceeded.disk_limit_exceeded  # hard limit → alert


def test_seconds_until_next_run(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, retention_purge_time_utc=dt.time(21, 30))
    job = RetentionJob(settings)

    before = dt.datetime(2026, 7, 16, 20, 30, tzinfo=dt.UTC)
    after = dt.datetime(2026, 7, 16, 22, 0, tzinfo=dt.UTC)

    assert job.seconds_until_next_run(before) == 3600.0
    assert job.seconds_until_next_run(after) == 23.5 * 3600  # zítra 21:30
