"""Kouč v1 (#1187 fáze 3, #933): denní review obchodů a týdenní report.

Čte obchody deníku (typ `obchod`, včetně paper obchodů z enginu) a bary
podkladu (pro „předčasný výstup"); výpočet je v `compute.coach` (čisté
funkce). Nic nezapisuje.
"""

import datetime as dt
from collections.abc import Callable, Sequence
from typing import Any

import pandas as pd
from fastapi import APIRouter

from gexlens_engine.compute.coach import (
    CoachParams,
    Trade,
    daily_review,
    trade_from_journal,
    weekly_report,
)
from gexlens_engine.compute.settle import trading_session_date
from gexlens_engine.compute.setups import SetupParams

JournalReader = Callable[[dt.date, dt.date], list[dict[str, Any]]]
BarsReader = Callable[[str, dt.date], pd.DataFrame]


def _bars_after_factory(
    bars_reader: BarsReader,
) -> Callable[[str, dt.date], Sequence[tuple[dt.datetime, float, float]]]:
    cache: dict[tuple[str, dt.date], list[tuple[dt.datetime, float, float]]] = {}

    def bars_after(symbol: str, day: dt.date) -> Sequence[tuple[dt.datetime, float, float]]:
        key = (symbol, day)
        if key not in cache:
            try:
                frame = bars_reader(symbol, day)
            except Exception:
                frame = pd.DataFrame()
            rows: list[tuple[dt.datetime, float, float]] = []
            if not frame.empty and {"ts_min", "high", "low"} <= set(frame.columns):
                for ts, high, low in zip(frame["ts_min"], frame["high"], frame["low"], strict=True):
                    stamp = pd.Timestamp(ts).to_pydatetime()
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=dt.UTC)
                    rows.append((stamp, float(high), float(low)))
            cache[key] = rows
        return cache[key]

    return bars_after


def build_coach_router(
    journal_reader: JournalReader,
    bars_reader: BarsReader,
    params_factory: Callable[[], SetupParams],
    *,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> APIRouter:
    router = APIRouter(prefix="/coach", tags=["coach"])

    def _params() -> CoachParams:
        setup_params = params_factory()
        return CoachParams(daily_brake_r=setup_params.daily_brake_r)

    def _trades(since: dt.date, until: dt.date) -> list[Trade]:
        rows = journal_reader(since, until)
        trades = [t for t in (trade_from_journal(row) for row in rows) if t is not None]
        return trades

    @router.get("/review")
    def review(date: dt.date | None = None, symbol: str | None = None) -> dict[str, Any]:
        """Denní review: obchody seance s příznaky, skóre disciplíny, shrnutí."""
        day = date or trading_session_date(now())
        # Předchozí dny kvůli revenge (vstup po stopu z minulé seance přes noc)
        trades = _trades(day - dt.timedelta(days=3), day)
        if symbol:
            trades = [t for t in trades if t.symbol == symbol]
        result = daily_review(
            trades, day, params=_params(), bars_after=_bars_after_factory(bars_reader)
        )
        return result.as_dict()

    @router.get("/weekly")
    def weekly(date: dt.date | None = None, symbol: str | None = None) -> dict[str, Any]:
        """Týdenní report (po–pá týdne obsahujícího `date`): příznaky s cenou, hodiny, pravidla."""
        day = date or trading_session_date(now())
        monday = day - dt.timedelta(days=day.weekday())
        trades = _trades(monday - dt.timedelta(days=3), monday + dt.timedelta(days=4))
        if symbol:
            trades = [t for t in trades if t.symbol == symbol]
        result = weekly_report(
            trades, day, params=_params(), bars_after=_bars_after_factory(bars_reader)
        )
        return result.as_dict()

    return router
