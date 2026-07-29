"""Testy track record (#298, SPEC 7.3): point-in-time, next-open, equity."""

import datetime as dt

import pytest

from gexlens_engine.compute.sentwaves import DailyClose, assess_state
from gexlens_news.track_record import (
    EquityPoint,
    SessionBar,
    Trade,
    equity_curve,
    evaluation_start,
    state_positions,
    trades_to_curve,
)


def day(offset: int) -> dt.date:
    return dt.date(2026, 1, 1) + dt.timedelta(days=offset)


def closes_from(values: list[float]) -> list[DailyClose]:
    return [DailyClose(date=day(index), close=value) for index, value in enumerate(values)]


def zigzag(cycles: int) -> list[DailyClose]:
    """Střídavé vlny nahoru/dolů — každý cyklus uzavře vlnu v obou směrech."""
    values: list[float] = []
    for _ in range(cycles):
        values.extend([1.0, 2.0, 3.0, 4.0, 3.0, 1.0, -1.0, -2.0, -3.0, -2.0, 0.5])
    return closes_from(values)


def test_evaluation_start_needs_wave_history_both_directions() -> None:
    """ADR-0021: dokud práh nemá z čeho žít, běží kalibrace — ne report."""
    assert evaluation_start(closes_from([1, 2, 3, 4, 5])) is None
    points = zigzag(4)
    start = evaluation_start(points)
    assert start is not None
    # Začátek je až po několika cyklech, ne hned po první vlně
    assert start > points[11].date


def test_state_positions_use_previous_close_state() -> None:
    """Pozice dne d = stav spočtený z closes ≤ d−1 (vstup na následující open)."""
    points = zigzag(5)
    start = points[30].date
    positions = state_positions(points, start=start)
    for index in range(1, len(points)):
        current = points[index].date
        if current < start:
            assert current not in positions
            continue
        state = assess_state(points[:index]).state
        expected = 1 if state == "RiskOn" else 0
        assert positions[current] == expected

    # short_riskoff přepíná RiskOff z flat na −1
    short = state_positions(points, start=start, short_riskoff=True)
    riskoff_days = [
        points[i].date
        for i in range(1, len(points))
        if points[i].date >= start and assess_state(points[:i]).state == "RiskOff"
    ]
    assert riskoff_days, "syntetická řada má mít RiskOff dny"
    assert all(short[d] == -1 for d in riskoff_days)


def test_equity_curve_buy_hold_telescopes_and_tracks_drawdown() -> None:
    bars = [
        SessionBar(date=day(0), open=100.0, close=110.0),
        SessionBar(date=day(1), open=112.0, close=99.0),
    ]
    points = equity_curve(bars)
    # Buy & hold: gap i seance se skládají → equity = close_n / open_0
    assert points[-1].equity == pytest.approx(99.0 / 100.0)
    assert points[0].drawdown == 0.0
    assert points[-1].drawdown == pytest.approx(99.0 / 110.0 - 1)


def test_equity_curve_splits_day_on_position_switch() -> None:
    """Den změny: stará pozice drží close→open, nová open→close (ADR-0021)."""
    bars = [
        SessionBar(date=day(0), open=100.0, close=110.0),
        SessionBar(date=day(1), open=120.0, close=126.0),
    ]
    # Den 0 flat, den 1 long: gap 110→120 je bez pozice, chytne se jen 120→126
    positions = {day(0): 0, day(1): 1}
    points = equity_curve(bars, positions)
    assert points[0].equity == 1.0
    assert abs(points[1].equity - 126.0 / 120.0) < 1e-12

    # Opačně: den 0 long, den 1 flat — gap se chytne (long až do open), pak nic
    positions = {day(0): 1, day(1): 0}
    points = equity_curve(bars, positions)
    assert abs(points[1].equity - (110.0 / 100.0) * (120.0 / 110.0)) < 1e-12


def test_trades_to_curve_carries_value_between_exits() -> None:
    sessions = [day(0), day(1), day(2)]
    trades = [Trade(exit_date=day(0), ret=0.10), Trade(exit_date=day(2), ret=-0.05)]
    points = trades_to_curve(trades, sessions)
    assert [round(p.equity, 6) for p in points] == [1.1, 1.1, round(1.1 * 0.95, 6)]
    assert points[1].drawdown == 0.0
    assert points[2].drawdown < 0.0
    assert isinstance(points[0], EquityPoint)
