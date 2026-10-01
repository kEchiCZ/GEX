"""Golden testy plánování hlubokého bar backfillu (#369)."""

import datetime as dt
from pathlib import Path

import pytest

from gexlens_engine.compute.settle import quarterly_expiry
from gexlens_engine.ibkr.deepbars import (
    CHUNK_CALENDAR_DAYS,
    FetchTask,
    FrontWindow,
    bucket_by_day,
    build_plan,
    chunk_tasks,
    contract_candidates,
    existing_days,
    front_windows,
    plan_day_refill,
    task_is_covered,
)
from gexlens_engine.ibkr.underlying import Bar
from gexlens_engine.storage.parquet_store import (
    BAR_SOURCE_HISTORICAL,
    BAR_SOURCE_LIVE,
    BAR_SOURCE_RECONSTRUCTED,
)

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


def test_contract_candidates_front_by_expiry_then_next_quarter() -> None:
    # Roll týden září 2026: engine do #1189 měřil U6, po něm Z6 — rozhodnou data
    assert contract_candidates(dt.date(2026, 9, 10)) == ["202609", "202612"]
    # Den expirace patří ještě dobíhajícímu kontraktu (jako front_windows)
    assert contract_candidates(dt.date(2026, 9, 18)) == ["202609", "202612"]
    assert contract_candidates(dt.date(2026, 9, 19)) == ["202612", "202703"]
    # Přes konec roku
    assert contract_candidates(dt.date(2026, 12, 31)) == ["202703", "202706"]


def _bar(minute: int, close: float, source: str | None = BAR_SOURCE_HISTORICAL) -> Bar:
    ts = dt.datetime(2026, 9, 10, 14, minute, tzinfo=dt.UTC)
    return Bar(ts=ts, open=close, high=close, low=close, close=close, volume=1, source=source)


def test_plan_day_refill_fills_holes_and_replaces_only_tasty() -> None:
    existing = {
        _bar(0, 0).ts: BAR_SOURCE_LIVE,
        _bar(1, 0).ts: None,  # NULL ze starých partic = živá cesta
        _bar(2, 0).ts: BAR_SOURCE_HISTORICAL,
        _bar(3, 0).ts: BAR_SOURCE_RECONSTRUCTED,
        _bar(4, 0).ts: BAR_SOURCE_RECONSTRUCTED,  # IBKR ji nedodá → zůstane
        # 14:05 chybí (díra)
        _bar(6, 0).ts: BAR_SOURCE_LIVE,
    }
    measured = {_bar(minute, 0).ts: 100.0 + minute for minute in (0, 1, 6)}
    incoming = [_bar(minute, 100.0 + minute) for minute in (6, 5, 3, 2, 1, 0)]

    plan = plan_day_refill(existing, incoming, measured=measured, replace_tasty=True)
    assert [bar.ts.minute for bar in plan.bars] == [3, 5]  # seřazené, jen tasty a díra
    assert (plan.filled, plan.replaced, plan.tasty_left) == (1, 1, 1)
    assert plan.rejected == []

    holes_only = plan_day_refill(existing, incoming, measured=measured, replace_tasty=False)
    assert [bar.ts.minute for bar in holes_only.bars] == [5]
    assert (holes_only.filled, holes_only.replaced, holes_only.tasty_left) == (1, 0, 2)


def test_plan_day_refill_rejects_block_next_to_other_contract() -> None:
    """Engine během dne přepnul kontrakt: medián dne vybere ten z většiny dne (#1320).

    Měřeno 14:00–14:09 a 14:15–14:19 kontraktem staženého dne (close 100),
    od 14:25 už jiným (close 101, +1 % basis). Blok vedle jiného kontraktu se
    nezapíše, blok bez měřené minuty po stranách také ne.
    """
    same = [*range(0, 10), *range(15, 20)]
    other = range(25, 30)
    existing: dict[dt.datetime, str | None] = {_bar(i, 0).ts: BAR_SOURCE_LIVE for i in same}
    existing |= {_bar(i, 0).ts: BAR_SOURCE_LIVE for i in other}
    existing |= {_bar(i, 0).ts: BAR_SOURCE_RECONSTRUCTED for i in [*range(10, 15), *range(30, 35)]}
    # 14:20–14:24 díra mezi oběma kontrakty; 14:50–14:52 díra daleko za vším
    measured = {_bar(i, 0).ts: 100.0 for i in same} | {_bar(i, 0).ts: 101.0 for i in other}
    incoming = [_bar(i, 100.0) for i in [*range(0, 35), *range(50, 53)]]

    plan = plan_day_refill(existing, incoming, measured=measured, replace_tasty=True)

    assert [bar.ts.minute for bar in plan.bars] == list(range(10, 15))
    assert (plan.filled, plan.replaced, plan.tasty_left) == (0, 5, 5)
    # 14:50–14:52: nejbližší měřená minuta vlevo je 14:29 (jiný kontrakt)
    assert [(block.start.minute, block.end.minute, block.minutes) for block in plan.rejected] == [
        (20, 24, 5),
        (30, 34, 5),
        (50, 52, 3),
    ]
    assert [block.deviation for block in plan.rejected] == pytest.approx([1 / 101] * 3)

    # Bez měřených minut nejde kontrakt ověřit u žádného bloku
    blind = plan_day_refill(existing, incoming, measured={}, replace_tasty=True)
    assert blind.bars == []
    assert [block.deviation for block in blind.rejected] == [None, None, None, None]
