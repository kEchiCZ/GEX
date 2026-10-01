"""Golden testy plánování hlubokého bar backfillu (#369)."""

import datetime as dt
from collections.abc import Mapping
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
    StoredBar,
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


def _at(hour: int, minute: int, close: float, source: str | None = BAR_SOURCE_HISTORICAL) -> Bar:
    """Bar 10. 9. 2026 v libovolné minutě (CDT: denní pauza 21:00–22:00 UTC)."""
    ts = dt.datetime(2026, 9, 10, hour, minute, tzinfo=dt.UTC)
    return Bar(ts=ts, open=close, high=close, low=close, close=close, volume=1, source=source)


def _stored(bar: Bar) -> StoredBar:
    return StoredBar(bar.ts, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.source)


def _partition(rows: Mapping[int, tuple[str | None, float]]) -> dict[dt.datetime, StoredBar]:
    """Uložená partice z minut → (source, close)."""
    bars = [_bar(minute, close, source) for minute, (source, close) in rows.items()]
    return {bar.ts: _stored(bar) for bar in bars}


def _written(
    existing: Mapping[dt.datetime, StoredBar], bars: list[Bar]
) -> dict[dt.datetime, StoredBar]:
    """Partice po upsertu plánu (zapisované bary `ibkr_hist` vyhrají nad `ibkr_hist`)."""
    return dict(existing) | {bar.ts: _stored(bar) for bar in bars}


def test_plan_day_refill_fills_holes_and_replaces_only_tasty() -> None:
    existing = _partition(
        {
            0: (BAR_SOURCE_LIVE, 100.0),
            1: (None, 101.0),  # NULL ze starých partic = živá cesta
            2: (BAR_SOURCE_HISTORICAL, 102.0),
            3: (BAR_SOURCE_RECONSTRUCTED, 0.0),
            4: (BAR_SOURCE_RECONSTRUCTED, 0.0),  # IBKR ji nedodá → zůstane
            # 14:05 chybí (díra)
            6: (BAR_SOURCE_LIVE, 106.0),
        }
    )
    measured = {_bar(minute, 0).ts: 100.0 + minute for minute in (0, 1, 6)}
    incoming = [_bar(minute, 100.0 + minute) for minute in (6, 5, 3, 2, 1, 0)]

    plan = plan_day_refill(existing, incoming, measured=measured, replace_tasty=True)
    assert [bar.ts.minute for bar in plan.bars] == [3, 5]  # seřazené, jen tasty a díra
    assert (plan.filled, plan.replaced, plan.tasty_left) == (1, 1, 1)
    assert plan.rejected == []
    assert plan.rewritten == []

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
    existing = _partition(
        {i: (BAR_SOURCE_LIVE, 100.0) for i in same}
        | {i: (BAR_SOURCE_LIVE, 101.0) for i in other}
        | {i: (BAR_SOURCE_RECONSTRUCTED, 100.0) for i in [*range(10, 15), *range(30, 35)]}
    )
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
    assert [block.reason for block in plan.rejected] == ["mismatch"] * 3

    # Bez měřených minut nejde kontrakt ověřit u žádného bloku
    blind = plan_day_refill(existing, incoming, measured={}, replace_tasty=True)
    assert blind.bars == []
    assert [block.deviation for block in blind.rejected] == [None, None, None, None]
    assert [block.reason for block in blind.rejected] == ["no_edge"] * 4


def test_plan_day_refill_rewrites_historical_of_other_contract() -> None:
    """Dřívější doplnění jiného kontraktu se přepíše barem ověřeného staženého (#1320).

    9. 9. 2026: mezi měřenými minutami U6 bloky `ibkr_hist` ze Z6 (skok o basis).
    Přepíše se jen `ibkr_hist` mimo toleranci; `ibkr_hist` v toleranci, měřená
    minuta (i odlišná) a rekonstrukce bez `replace_tasty` zůstávají.
    """
    existing = _partition(
        {i: (BAR_SOURCE_LIVE, 100.0) for i in [*range(0, 5), 11, 13, 14, *range(16, 20)]}
        | {i: (BAR_SOURCE_HISTORICAL, 101.0) for i in (5, 6, 7)}  # jiný kontrakt, +1 %
        | {8: (BAR_SOURCE_HISTORICAL, 100.2)}  # v toleranci 0,3 % → zůstane, dělí bloky
        | {i: (BAR_SOURCE_HISTORICAL, 98.5) for i in (9, 10)}  # jiný kontrakt, −1,5 %
        | {12: (BAR_SOURCE_LIVE, 103.0)}  # měřená minuta se nepřepisuje nikdy
        | {15: (BAR_SOURCE_RECONSTRUCTED, 100.4)}
    )
    measured = {ts: bar.close for ts, bar in existing.items() if bar.source == BAR_SOURCE_LIVE}
    incoming = [_bar(i, 100.0) for i in range(20)]

    plan = plan_day_refill(
        existing, incoming, measured=measured, replace_tasty=False, replace_wrong_contract=True
    )

    assert [bar.ts.minute for bar in plan.bars] == [5, 6, 7, 9, 10]
    assert all(bar.close == 100.0 and bar.source == BAR_SOURCE_HISTORICAL for bar in plan.bars)
    assert (plan.filled, plan.replaced, plan.tasty_left) == (0, 0, 1)
    assert plan.rejected == []
    assert [(b.start.minute, b.end.minute, b.minutes) for b in plan.rewritten] == [
        (5, 7, 3),
        (9, 10, 2),
    ]
    assert [b.deviation for b in plan.rewritten] == pytest.approx([0.01, 0.015])

    # Bez volby se dřívější doplnění nepřepisuje (chování --days před #1320)
    kept = plan_day_refill(existing, incoming, measured=measured, replace_tasty=False)
    assert (kept.bars, kept.rewritten) == ([], [])

    # Idempotence: po zápisu plánu už nic mimo toleranci nezbývá
    again = plan_day_refill(
        _written(existing, plan.bars),
        incoming,
        measured=measured,
        replace_tasty=False,
        replace_wrong_contract=True,
    )
    assert (again.bars, again.rewritten, again.rejected) == ([], [], [])


def test_plan_day_refill_ignores_measured_minute_across_daily_pause() -> None:
    """Měřená minuta za denní pauzou je jiná seance a kontrakt bloku neověří (#1320).

    Rolovací den: engine měřil kontrakt A (close 100) do 20:59 UTC, s novou
    seancí (22:00 UTC = obchodní den 11. 9.) přešel discovery na B a konec
    partice je `ibkr_hist` z B (close 101). Medián dne vybere A. Kdyby se
    počítala minuta 20:59 před pauzou, přepsaly by se správné bary B na A
    a skok o basis by se přesunul z pauzy na půlnoc doprostřed seance. Díra
    ve stejné seanci se ze stejného důvodu nedoplní.
    """
    live_a = [(20, m) for m in range(50, 60)]
    hist_b = [(22, m) for m in range(10)]
    hole = [(22, m) for m in range(20, 30)]
    existing = {
        bar.ts: _stored(bar)
        for bar in [_at(h, m, 100.0, BAR_SOURCE_LIVE) for h, m in live_a]
        + [_at(h, m, 101.0) for h, m in hist_b]
    }
    measured = {_at(h, m, 0).ts: 100.0 for h, m in live_a}
    incoming = [_at(h, m, 100.0) for h, m in [*live_a, *hist_b, *hole]]

    plan = plan_day_refill(
        existing, incoming, measured=measured, replace_tasty=True, replace_wrong_contract=True
    )

    assert (plan.bars, plan.rewritten, plan.filled) == ([], [], 0)
    assert [
        (b.start.strftime("%H:%M"), b.end.strftime("%H:%M"), b.deviation, b.reason)
        for b in plan.rejected
    ] == [("22:00", "22:09", None, "no_edge"), ("22:20", "22:29", None, "no_edge")]


def test_plan_day_refill_rewrite_needs_measured_minute_on_both_sides() -> None:
    """Přepis `ibkr_hist` potřebuje měřenou minutu téže seance po obou stranách (#1320).

    V seanci od 22:00 UTC měřil engine kontrakt A až od 22:10. Před tím mohl
    měřit starší kontrakt a restartem přejít na A — `ibkr_hist` 22:00–22:09
    (close 101) pak je správný a přepis barem A by ho zničil. Doplnění díry na
    stejném místě stačí jedna ověřená strana (chování před #1320).
    """
    live_a = [(20, m) for m in range(50, 60)] + [(22, m) for m in range(10, 20)]
    block = [(22, m) for m in range(10)]
    measured = {_at(h, m, 0).ts: 100.0 for h, m in live_a}
    incoming = [_at(h, m, 100.0) for h, m in [*live_a, *block]]
    live = {bar.ts: _stored(bar) for bar in [_at(h, m, 100.0, BAR_SOURCE_LIVE) for h, m in live_a]}

    rewrite = plan_day_refill(
        live | {bar.ts: _stored(bar) for bar in [_at(h, m, 101.0) for h, m in block]},
        incoming,
        measured=measured,
        replace_tasty=False,
        replace_wrong_contract=True,
    )
    assert (rewrite.bars, rewrite.rewritten) == ([], [])
    assert [(b.start.minute, b.end.minute, b.deviation, b.reason) for b in rewrite.rejected] == [
        (0, 9, 0.0, "one_edge")
    ]

    fill = plan_day_refill(live, incoming, measured=measured, replace_tasty=False)
    assert [bar.ts.strftime("%H:%M") for bar in fill.bars] == [f"22:0{m}" for m in range(10)]
    assert (fill.filled, fill.rejected) == (10, [])


def test_plan_day_refill_does_not_rewrite_next_to_other_contract() -> None:
    """Přepis se odmítne, když engine vedle bloku měřil jiný kontrakt (#1320).

    Měřeno A do 14:04 a B od 14:10 (roll restartem), mezi nimi `ibkr_hist`
    z B, stažený den je A. Medián by A pustil; blok sousedí s B, takže zůstane.
    """
    a_live, b_live, hist_b = range(0, 5), range(10, 15), range(5, 10)
    existing = _partition(
        {i: (BAR_SOURCE_LIVE, 100.0) for i in a_live}
        | {i: (BAR_SOURCE_LIVE, 101.0) for i in b_live}
        | {i: (BAR_SOURCE_HISTORICAL, 101.0) for i in hist_b}
    )
    measured = {ts: bar.close for ts, bar in existing.items() if bar.source == BAR_SOURCE_LIVE}
    incoming = [_bar(i, 100.0) for i in range(15)]

    plan = plan_day_refill(
        existing, incoming, measured=measured, replace_tasty=False, replace_wrong_contract=True
    )

    assert (plan.bars, plan.rewritten) == ([], [])
    assert [(b.start.minute, b.end.minute, b.reason) for b in plan.rejected] == [(5, 9, "mismatch")]
    assert plan.rejected[0].deviation == pytest.approx(1 / 101)
