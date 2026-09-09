"""Vyhodnocení verdiktů dne po settle (#1091, ADR-0035 §3) — po vzoru `volregime`.

Verdikt (spíše long / spíše short / bez převahy / počkat na tisk) ukládá
frontend před seancí. Tady se po settle doplní, jak seance dopadla:
pohyb od US openu (9:30 ET) do settle v bodech i v násobcích expected move
(EM z `em_respect`, #872) a zásah verdiktu. Bez US openu (svátek, díra
v barech) se bere otevření Globex seance. „Počkat na tisk" se nehodnotí,
„bez převahy" jen když je EM (range den = close do ±0,5 EM).

Čte jen bary z partic — žádná IBKR linka navíc.
"""

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.engine import Engine

from gexlens_engine.compute.settle import (
    ET_TZ,
    session_bounds,
    session_time_utc,
    settle_ts,
    trading_session_date,
)
from gexlens_engine.storage.briefing_verdicts_store import (
    BriefingVerdictRepository,
    VerdictOutcome,
)
from gexlens_engine.storage.emrespect_store import em_respect_table

logger = logging.getLogger(__name__)

#: Odklad po settle — bary poslední minuty musí stihnout dorazit (jako volregime)
SETTLE_GRACE_MINUTES = 15
#: „Bez převahy" trefil range den: close do ±tolik EM od US openu
RANGE_DAY_EM = 0.5
US_OPEN_LOCAL = dt.time(9, 30)


@dataclass(frozen=True)
class SessionOhlc:
    open: float
    us_open: float | None
    close: float


def session_ohlc(data_dir: Path, symbol: str, session: dt.date) -> SessionOhlc | None:
    """Otevření Globex seance, otevření US RTH a close do settle z bars partic."""
    bars_dir = data_dir / "derived" / symbol / "bars"
    start, _end = session_bounds(session)
    settle = settle_ts(session)
    us_open_ts = session_time_utc(session, US_OPEN_LOCAL.hour, US_OPEN_LOCAL.minute, ET_TZ)
    rows: list[tuple[dt.datetime, float, float]] = []
    for day in (session - dt.timedelta(days=1), session):
        path = bars_dir / f"{day.isoformat()}.parquet"
        if not path.exists():
            continue
        try:
            table = pq.read_table(path, columns=["ts_min", "open", "close"])
        except Exception:
            logger.exception("Bars partice %s nečitelná — přeskočena", path)
            continue
        for record in table.to_pylist():
            ts = record["ts_min"]
            if ts is None or ts < start or ts > settle:
                continue
            rows.append((ts, float(record["open"]), float(record["close"])))
    if not rows:
        return None
    rows.sort(key=lambda row: row[0])
    us_open = next((open_ for ts, open_, _ in rows if ts >= us_open_ts), None)
    return SessionOhlc(open=rows[0][1], us_open=us_open, close=rows[-1][2])


def evaluate_verdict(verdict: str, ohlc: SessionOhlc, em_points: float | None) -> VerdictOutcome:
    """Zásah verdiktu podle pohybu od US openu (fallback Globex open) do settle."""
    base = ohlc.us_open if ohlc.us_open is not None else ohlc.open
    move = ohlc.close - base
    move_em = move / em_points if em_points else None
    hit: bool | None
    if verdict == "long":
        hit = move > 0
    elif verdict == "short":
        hit = move < 0
    elif verdict == "none":
        hit = abs(move_em) <= RANGE_DAY_EM if move_em is not None else None
    else:
        hit = None  # wait_news se nehodnotí
    return VerdictOutcome(
        open=ohlc.open,
        us_open=ohlc.us_open,
        close=ohlc.close,
        move_pts=move,
        move_em=move_em,
        hit=hit,
    )


@dataclass
class BriefingVerdictCollector:
    """Jednou po settle doplní výsledek ke všem verdiktům ukončených seancí."""

    symbol: str
    repository: BriefingVerdictRepository
    db: Engine
    data_dir: Path
    _evaluated_for: dt.date | None = field(default=None, init=False)

    async def on_minute(self, now: dt.datetime) -> None:
        session = trading_session_date(now)
        boundary = settle_ts(session) + dt.timedelta(minutes=SETTLE_GRACE_MINUTES)
        if now < boundary or self._evaluated_for == session:
            return
        self._evaluated_for = session  # jeden pokus per seance i při chybě
        await asyncio.to_thread(self._run, session, now)

    def _run(self, session: dt.date, now: dt.datetime) -> int:
        pending = self.repository.pending(self.symbol, before=session + dt.timedelta(days=1))
        done = 0
        for row in pending:
            session_date = row["session_date"]
            if isinstance(session_date, str):
                session_date = dt.date.fromisoformat(session_date)
            ohlc = session_ohlc(self.data_dir, self.symbol, session_date)
            if ohlc is None:
                logger.info(
                    "Verdikt %s %s bez barů seance — výsledek se nedoplní",
                    self.symbol,
                    session_date,
                )
                continue
            outcome = evaluate_verdict(str(row["verdict"]), ohlc, self._em_points(session_date))
            self.repository.update_outcome(int(row["id"]), outcome, now)
            done += 1
        if done:
            logger.info("Verdikty dne %s: doplněn výsledek u %d seancí", self.symbol, done)
        return done

    def _em_points(self, session: dt.date) -> float | None:
        stmt = select(em_respect_table.c.em_points).where(
            em_respect_table.c.symbol == self.symbol,
            em_respect_table.c.session_date == session,
        )
        with self.db.connect() as conn:
            value: Any = conn.execute(stmt).scalar_one_or_none()
        return float(value) if isinstance(value, int | float) else None
