"""Golden testy plánování hlubokého bar backfillu (#369)."""

import datetime as dt
from pathlib import Path

from gexlens_engine.ibkr.deepbars import (
    CHUNK_CALENDAR_DAYS,
    FetchTask,
    FrontWindow,
    bucket_by_day,
    build_plan,
    chunk_tasks,
    existing_days,
    front_windows,
    quarterly_expiry,
    task_is_covered,
)
from gexlens_engine.ibkr.underlying import Bar

TODAY = dt.date(2026, 7, 29)


def test_quarterly_expiry_is_third_friday() -> None:
    assert quarterly_expiry(2026, 9) == dt.date(2026, 9, 18)
    assert quarterly_expiry(2026, 6) == dt.date(2026, 6, 19)
    assert quarterly_expiry(2025, 3) == dt.date(2025, 3, 21)
    assert quarterly_expiry(2024, 12) == dt.date(2024, 12, 20)


def test_front_windows_cover_horizon_without_gaps_or_today() -> None:
    windows = front_windows(730, today=TODAY)

    # Souvislé pokrytí: každé okno navazuje den po konci předchozího
    for previous, current in zip(windows, windows[1:], strict=False):
        assert current.start == previous.end + dt.timedelta(days=1)
    # Horizont: začátek prvního okna = today - depth, konec posledního = včera
    assert windows[0].start == TODAY - dt.timedelta(days=730)
    assert windows[-1].end == TODAY - dt.timedelta(days=1)
    # Aktuální front (ESU6, expirace 18. 9. 2026) končí VČEREJŠKEM, ne expirací
    assert windows[-1].contract_month == "202609"
    # Hranice mezi kontrakty = den po expiraci
    june = next(w for w in windows if w.contract_month == "202606")
    assert june.end == quarterly_expiry(2026, 6)
    assert june.start == quarterly_expiry(2026, 3) + dt.timedelta(days=1)


def test_chunk_tasks_cover_window_with_overlap() -> None:
    window = FrontWindow(
        contract_month="202606", start=dt.date(2026, 3, 21), end=dt.date(2026, 6, 19)
    )
    tasks = chunk_tasks("ES", window)

    # První chunk končí CHUNK dní po startu, poslední přesně na konci okna
    assert tasks[0].end == window.start + dt.timedelta(days=CHUNK_CALENDAR_DAYS - 1)
    assert tasks[-1].end == window.end
    # Každý den okna je pokrytý aspoň jedním chunkem
    covered: set[dt.date] = set()
    for task in tasks:
        day = task.span_start
        while day <= task.end:
            covered.add(day)
            day += dt.timedelta(days=1)
    day = window.start
    while day <= window.end:
        assert day in covered, day
        day += dt.timedelta(days=1)


def test_build_plan_scales_with_symbols() -> None:
    plan_one = build_plan(["ES"], 365, today=TODAY)
    plan_two = build_plan(["ES", "NQ"], 365, today=TODAY)
    assert len(plan_two) == 2 * len(plan_one)
    # ~365/12 chunků na symbol — sanity rozsahu (throttle plán ~30 min na 2 roky)
    assert 28 <= len(plan_one) <= 36


def test_existing_days_and_coverage(tmp_path: Path) -> None:
    bars_dir = tmp_path / "ES" / "bars"
    bars_dir.mkdir(parents=True)
    # Pracovní dny 13.–24. 7. 2026 (po–pá dva týdny) — víkendy chybí schválně
    day = dt.date(2026, 7, 13)
    while day <= dt.date(2026, 7, 24):
        if day.weekday() < 5:
            (bars_dir / f"{day.isoformat()}.parquet").touch()
        day += dt.timedelta(days=1)
    (bars_dir / "nesmysl.parquet").touch()  # nečitelný název nesmí shodit sken

    existing = existing_days(tmp_path, "ES")
    assert dt.date(2026, 7, 13) in existing
    assert dt.date(2026, 7, 18) not in existing  # sobota

    # Chunk plně pokrytý pracovními dny s particemi → přeskočit
    covered = FetchTask(symbol="ES", contract_month="202609", end=dt.date(2026, 7, 24))
    assert task_is_covered(covered, existing)
    # Chunk sahající před pokryté období → stáhnout
    uncovered = FetchTask(symbol="ES", contract_month="202609", end=dt.date(2026, 7, 15))
    assert not task_is_covered(uncovered, existing)


def test_bucket_by_day_splits_on_utc_midnight() -> None:
    def bar(ts: dt.datetime) -> Bar:
        return Bar(ts=ts, open=1, high=1, low=1, close=1, volume=0)

    buckets = bucket_by_day(
        [
            bar(dt.datetime(2026, 7, 28, 23, 59, tzinfo=dt.UTC)),
            bar(dt.datetime(2026, 7, 29, 0, 0, tzinfo=dt.UTC)),
            bar(dt.datetime(2026, 7, 29, 0, 1, tzinfo=dt.UTC)),
        ]
    )
    assert sorted(buckets) == [dt.date(2026, 7, 28), dt.date(2026, 7, 29)]
    assert len(buckets[dt.date(2026, 7, 29)]) == 2
