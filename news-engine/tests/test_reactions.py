"""Testy měření reakce (#276): okna, kontaminace, deferred gap, volume z-score."""

import datetime as dt

import pytest

from gexlens_news.reactions import (
    MIN_BASELINE_SESSIONS,
    Bar,
    SessionDaily,
    VolumeBaseline,
    build_volume_baseline,
    compute_daily_reactions,
    compute_reactions,
    volume_z_score,
)

EVENT = dt.datetime(2026, 7, 28, 14, 30, tzinfo=dt.UTC)


def bars(
    start: dt.datetime, closes: list[float], *, volume: float = 100.0, step_min: int = 1
) -> list[Bar]:
    return [
        Bar(
            ts=start + dt.timedelta(minutes=i * step_min),
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=volume,
        )
        for i, close in enumerate(closes)
    ]


def flat_baseline(sessions: int = MIN_BASELINE_SESSIONS) -> dict[dt.time, VolumeBaseline]:
    return {
        dt.time(hour=h, minute=m): VolumeBaseline(mean=100.0, variance=25.0, sessions=sessions)
        for h in range(24)
        for m in range(60)
    }


# ── Základní měření ────────────────────────────────────────────────


def test_windows_measure_from_price_before_the_news() -> None:
    """Základ je poslední close PŘED zprávou — jinak by se pohyb ztratil."""
    # 14:25–14:29 rovina na 7000, po zprávě růst
    history = bars(EVENT - dt.timedelta(minutes=5), [7000.0] * 5)
    after = bars(EVENT, [7007.0, 7014.0, 7014.0, 7014.0, 7014.0])
    reactions = compute_reactions(EVENT, history + after, windows=(1, 5))

    by_window = {r.window_min: r for r in reactions}
    # +1 min: jediný bar okna je 14:30 (close 7007) → +10 bps ze 7000
    assert by_window[1].ret_bp == pytest.approx(10.0)
    # +5 min: poslední bar okna je 14:34 (close 7014) → +20 bps
    assert by_window[5].ret_bp == pytest.approx(20.0)
    assert all(not r.deferred for r in reactions)
    assert all(not r.contaminated for r in reactions)


def test_range_is_measured_within_the_window() -> None:
    history = bars(EVENT - dt.timedelta(minutes=2), [7000.0, 7000.0])
    after = bars(EVENT, [7000.0, 7010.0])  # high 7011, low 6999
    reaction = compute_reactions(EVENT, history + after, windows=(5,))[0]
    assert reaction.range_bp == pytest.approx((7011.0 - 6999.0) / 7000.0 * 10_000)


def test_no_bars_means_no_reaction() -> None:
    assert compute_reactions(EVENT, [], windows=(5,)) == []
    # Jen historie bez baru po zprávě → není co měřit
    assert compute_reactions(EVENT, bars(EVENT - dt.timedelta(minutes=3), [1.0] * 3)) == []


# ── Kontaminace (anti-šum) ─────────────────────────────────────────


def test_contamination_flags_only_windows_that_contain_another_event() -> None:
    """Fed day: krátká okna zůstávají čistá, dlouhá chytí další zprávu."""
    series = bars(EVENT - dt.timedelta(minutes=5), [7000.0] * 70)
    other = EVENT + dt.timedelta(minutes=12)  # další high-impact event

    reactions = {
        r.window_min: r
        for r in compute_reactions(EVENT, series, windows=(1, 5, 15, 60), other_event_ts=[other])
    }
    assert not reactions[1].contaminated
    assert not reactions[5].contaminated
    assert reactions[15].contaminated
    assert reactions[60].contaminated


def test_event_outside_window_does_not_contaminate() -> None:
    series = bars(EVENT - dt.timedelta(minutes=2), [7000.0] * 20)
    before = EVENT - dt.timedelta(minutes=1)  # dřívější event okno nekazí
    after = EVENT + dt.timedelta(minutes=90)
    reactions = compute_reactions(EVENT, series, windows=(1, 5), other_event_ts=[before, after])
    assert all(not r.contaminated for r in reactions)


# ── Deferred (víkend / zavřený trh) ────────────────────────────────


def test_weekend_event_measures_from_first_traded_bar_but_keeps_the_gap() -> None:
    """Sobotní zpráva: okno běží od nedělního open, ret_bp zahrnuje gap.

    Přesně tím se systém učí, co víkendové titulky dělají s pondělním open
    (SPEC 5.1) — kdyby se základ posunul na open, gap by se ztratil.
    """
    friday_close = bars(EVENT - dt.timedelta(minutes=2), [7000.0, 7000.0])
    # Trh otevře až za 2 dny, hned o 70 bodů výš
    sunday = bars(EVENT + dt.timedelta(days=2), [7070.0, 7070.0, 7070.0])
    reactions = compute_reactions(EVENT, friday_close + sunday, windows=(1, 5))

    assert all(r.deferred for r in reactions)
    # +100 bps = celý gap ze 7000 na 7070
    assert reactions[0].ret_bp == pytest.approx(100.0)


def test_short_data_gap_is_not_treated_as_closed_market() -> None:
    """Jeden chybějící bar není zavřený trh — jinak by se deferred rozlilo."""
    history = bars(EVENT - dt.timedelta(minutes=2), [7000.0, 7000.0])
    after = bars(EVENT + dt.timedelta(minutes=2), [7000.0, 7000.0, 7000.0])
    reactions = compute_reactions(EVENT, history + after, windows=(5,))
    assert not reactions[0].deferred


# ── Objemové z-score ───────────────────────────────────────────────


def test_volume_z_score_normalises_against_same_time_of_day() -> None:
    history = bars(EVENT - dt.timedelta(minutes=2), [7000.0, 7000.0])
    # Pětiminutové okno s dvojnásobným objemem proti baseline 100/min
    after = bars(EVENT, [7000.0] * 5, volume=200.0)
    reaction = compute_reactions(EVENT, history + after, windows=(5,), baseline=flat_baseline())[0]
    # očekávaný objem 5×100 = 500, σ = sqrt(5×25) ≈ 11.18, skutečnost 1000
    assert reaction.vol_z == pytest.approx((1000 - 500) / (5 * 25) ** 0.5)


def test_volume_z_score_is_none_without_enough_sessions() -> None:
    """Radši žádná hodnota než z-score z pár dní (SPEC chce 20 seancí)."""
    window = bars(EVENT, [7000.0] * 3)
    assert volume_z_score(window, flat_baseline(sessions=5)) is None
    assert volume_z_score(window, None) is None
    assert volume_z_score([], flat_baseline()) is None
    # Chybějící minuta v baseline taky diskvalifikuje celé okno
    partial = flat_baseline()
    del partial[EVENT.time()]
    assert volume_z_score(window, partial) is None


def test_build_volume_baseline_averages_per_minute_of_day() -> None:
    day1 = bars(dt.datetime(2026, 7, 27, 14, 30, tzinfo=dt.UTC), [1.0, 1.0], volume=100.0)
    day2 = bars(dt.datetime(2026, 7, 28, 14, 30, tzinfo=dt.UTC), [1.0, 1.0], volume=300.0)
    baseline = build_volume_baseline([day1, day2])

    stats = baseline[dt.time(14, 30)]
    assert stats.mean == pytest.approx(200.0)
    assert stats.sessions == 2
    assert stats.variance == pytest.approx(10_000.0)  # ((100-200)² + (300-200)²)/2


def test_reaction_without_baseline_still_produces_ret_and_range() -> None:
    """Chybějící objemová baseline nesmí zahodit celé měření."""
    series = bars(EVENT - dt.timedelta(minutes=2), [7000.0] * 10)
    reaction = compute_reactions(EVENT, series, windows=(5,))[0]
    assert reaction.vol_z is None
    assert reaction.ret_bp == pytest.approx(0.0)
    assert reaction.range_bp > 0


# ── Denní okna (#564) ──────────────────────────────────────────────


def daily_sessions(first_day: dt.date, closes: list[float]) -> list[SessionDaily]:
    """Seance po sobě, settle 20:00 UTC, high/low = close ± 10."""
    return [
        SessionDaily(
            day=first_day + dt.timedelta(days=index),
            settle_ts=dt.datetime.combine(
                first_day + dt.timedelta(days=index), dt.time(20, 0), tzinfo=dt.UTC
            ),
            close=close,
            high=close + 10,
            low=close - 10,
        )
        for index, close in enumerate(closes)
    ]


def test_daily_1d_je_nejblizsi_settle_po_udalosti() -> None:
    day = dt.date(2026, 7, 28)
    sessions = daily_sessions(day, [7010.0, 7020.0, 7030.0])
    around = bars(dt.datetime(2026, 7, 28, 14, 0, tzinfo=dt.UTC), [7000.0] * 60)
    # Ranní zpráva: 1d = settle TÉŽE seance, 2d = další
    morning = dt.datetime(2026, 7, 28, 14, 30, tzinfo=dt.UTC)
    reactions = compute_daily_reactions(morning, around, sessions, window_days=(1, 2))
    assert [r.window_min for r in reactions] == [1440, 2880]
    assert reactions[0].ret_bp == pytest.approx((7010.0 - 7000.0) / 7000.0 * 10_000)
    assert reactions[1].ret_bp == pytest.approx((7020.0 - 7000.0) / 7000.0 * 10_000)
    # Zpráva PO settle: 1d = settle následující seance
    evening_bars = bars(dt.datetime(2026, 7, 28, 20, 0, tzinfo=dt.UTC), [7000.0] * 90)
    evening = dt.datetime(2026, 7, 28, 20, 30, tzinfo=dt.UTC)
    late = compute_daily_reactions(evening, evening_bars, sessions, window_days=(1,))
    assert late[0].ret_bp == pytest.approx((7020.0 - 7000.0) / 7000.0 * 10_000)


def test_daily_vraci_jen_uzavrena_okna_a_spravne_flagy() -> None:
    day = dt.date(2026, 7, 28)
    sessions = daily_sessions(day, [7010.0, 7020.0])
    around = bars(dt.datetime(2026, 7, 28, 14, 0, tzinfo=dt.UTC), [7000.0] * 60)
    event = dt.datetime(2026, 7, 28, 14, 30, tzinfo=dt.UTC)
    reactions = compute_daily_reactions(event, around, sessions, window_days=(1, 2, 5))
    # 5d nejde uzavřít (jen 2 seance) — vrací se jen 1d a 2d
    assert [r.window_min for r in reactions] == [1440, 2880]
    for reaction in reactions:
        assert reaction.contaminated is False  # v denním horizontu se neměří izolace
        assert reaction.vol_z is None
        assert reaction.deferred is False
    # range ze session high/low: max high 7030, min low 7000 → 30 b nad základnou 7000
    assert reactions[1].range_bp == pytest.approx((7030.0 - 7000.0) / 7000.0 * 10_000)


def test_daily_vikendova_zprava_je_deferred_a_zahrnuje_gap() -> None:
    saturday = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.UTC)
    # Poslední bar v pátek, první obchodovaný až v neděli večer — gap v základně
    friday_bars = bars(dt.datetime(2026, 7, 24, 19, 0, tzinfo=dt.UTC), [7000.0] * 30)
    sunday_bars = bars(dt.datetime(2026, 7, 26, 22, 0, tzinfo=dt.UTC), [7100.0] * 30)
    sessions = daily_sessions(dt.date(2026, 7, 27), [7150.0])
    reactions = compute_daily_reactions(
        saturday, [*friday_bars, *sunday_bars], sessions, window_days=(1,)
    )
    assert reactions[0].deferred is True
    assert reactions[0].ret_bp == pytest.approx((7150.0 - 7000.0) / 7000.0 * 10_000)
