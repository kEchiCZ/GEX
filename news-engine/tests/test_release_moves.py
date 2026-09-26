"""Velikost pohybu po releasu (#1296): měření, vol_ref a statistika násobků — čisté funkce."""

import datetime as dt

import pytest

from gexlens_news.reactions import Bar, compute_reactions, measure_excursion
from gexlens_news.release_moves import (
    MIN_HISTORY,
    history_count,
    measure_move,
    size_stats,
    vol_ref_bp,
)

UTC = dt.UTC
T = dt.datetime(2026, 9, 11, 12, 30, tzinfo=UTC)
MINUTE = dt.timedelta(minutes=1)


def bars_around(
    start: dt.datetime,
    *,
    before: int = 70,
    after: int = 61,
    jump_at: int = 3,
    jump: float = 20.0,
    skip: set[int] | None = None,
) -> list[Bar]:
    """Bary kolem releasu: mírný šum, od minuty `jump_at` po releasu skok o `jump` bodů."""
    rows = []
    for offset in range(-before, after):
        if skip and offset in skip:
            continue
        base = 7800.0 + (0.25 if offset % 2 else 0.0)
        close = base + (jump if offset >= jump_at else 0.0)
        rows.append(
            Bar(
                ts=start + offset * MINUTE,
                open=close,
                high=close + 0.5,
                low=close - 0.5,
                close=close,
                volume=10.0,
            )
        )
    return rows


def test_mereni_sedi_s_funkcemi_vyzkumu() -> None:
    bars = bars_around(T)
    measured = measure_move(bars, T)
    excursion = measure_excursion(bars, T, 15)
    assert excursion is not None
    assert measured.exc_15m_bp == pytest.approx(excursion.bp)
    reactions = {r.window_min: r for r in compute_reactions(T, bars, windows=(15, 60))}
    assert measured.ret_15m_bp == pytest.approx(reactions[15].ret_bp)
    assert measured.ret_60m_bp == pytest.approx(reactions[60].ret_bp)
    assert measured.base_close == pytest.approx(7800.25)


def test_bez_baru_minuty_pred_releasem_nic() -> None:
    measured = measure_move(bars_around(T, skip={-1}), T)
    assert measured == type(measured)(None, None, None, None)
    assert measure_move([], T).exc_15m_bp is None


def test_zavreny_trh_ani_dira_v_okne_se_nezapocte() -> None:
    # Jedna chybějící minuta se toleruje (výzkum: aspoň h − 1 barů v okně), dvě ne
    one = measure_move(bars_around(T, skip={5}), T)
    assert one.ret_15m_bp is not None and one.ret_60m_bp is not None
    two = measure_move(bars_around(T, skip={5, 6}), T)
    assert two.ret_15m_bp is None and two.ret_60m_bp is None and two.exc_15m_bp is None
    # Okno 60 min končí dřív (trh zavřel) → výnos 60 min None
    short = measure_move(bars_around(T, after=30), T)
    assert short.ret_60m_bp is None and short.ret_15m_bp is not None


def test_vol_ref_bere_20_seanci_pred_seanci_releasu() -> None:
    session = dt.date(2026, 9, 11)
    ranges = [(session - dt.timedelta(days=k), 70.0 + k) for k in range(30, 0, -1)]
    ranges.append((session, 500.0))  # seance releasu se nepočítá
    expected = 80.5  # medián rozsahů 71–90 (20 seancí těsně před releasem)
    assert vol_ref_bp(ranges, session, 7805.5) == pytest.approx(expected / 7805.5 * 1e4)
    assert vol_ref_bp(ranges[-15:], session, 7805.5) is None  # jen 14 seancí před
    assert vol_ref_bp(ranges, session, None) is None
    # Příklad z návrhu: medián 74,5 b. při ceně 7805,5 → 95,4 bp
    flat = [(session - dt.timedelta(days=k), 74.5) for k in range(20, 0, -1)]
    assert vol_ref_bp(flat, session, 7805.5) == pytest.approx(95.45, abs=0.01)


def test_size_stats_nasobky_a_prepocet() -> None:
    rows: list[tuple[float | None, float | None]] = [
        (40.0 * (1 + k / 10), 100.0) for k in range(10)
    ]
    rows += [(None, 100.0), (30.0, None), (25.0, 0.0)]  # do n se nepočítají
    stats = size_stats(rows)
    assert stats is not None
    assert stats.n == 10
    assert stats.mult_p50 == pytest.approx(0.58)
    assert stats.mult_p75 == pytest.approx(0.67)
    assert stats.raw_p50_bp == pytest.approx(58.0)
    low, high = stats.expected_bp(50.0)
    assert low == pytest.approx(29.0) and high == pytest.approx(33.5)
    assert stats.vol_ratio(50.0) == pytest.approx(0.5)
    assert history_count(rows) == 10


def test_size_stats_malo_historie() -> None:
    rows: list[tuple[float | None, float | None]] = [(40.0, 100.0)] * (MIN_HISTORY - 1)
    assert size_stats(rows) is None
    assert history_count(rows) == MIN_HISTORY - 1
