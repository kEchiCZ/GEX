"""Sběrač gamma útesu (#576, fáze 1): denní záznam po settle + backfill.

Vzor T6Collector: `on_minute` se volá z minutové smyčky a sám se hlídá na
jeden běh per seance. Nic nezapíná — jen měří (rozhodnutí uživatele).

Zdroj dat: levels partice per expirace (`derived/{sym}/{expiry}/levels/…`),
které nesou `total_gex`, flip a zdi per minuta — stav k settle je poslední
řádek ≤ settle. Zbytkový profil zná sekundární sweep (PR #94/#95) už před
settle. Backfill sahá tak daleko jako levels retence (~90 dní, ADR-0022).
"""

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.engine import Engine

from gexlens_engine.compute.gammacliff import (
    CliffRecord,
    ExpiryAtSettle,
    build_cliff,
    range_in_atr,
)
from gexlens_engine.compute.settle import session_bounds, settle_ts, trading_session_date
from gexlens_engine.storage.gammacliff_store import GammaCliffRepository
from gexlens_engine.storage.setups_store import setups_table

logger = logging.getLogger(__name__)

# Kolik minut po settle se počítá — ať poslední minutový zápis levels dosedne
SETTLE_GRACE_MINUTES = 5


def read_expiries_at(
    data_dir: Path, symbol: str, session_date: dt.date, at_ts: dt.datetime
) -> list[ExpiryAtSettle]:
    """Stav sledovaných expirací k okamžiku `at_ts` z levels partic seance.

    Bere expirace s datem ≥ seance (settlující + přeživší); poslední řádek
    partice s `ts_min` ≤ `at_ts`. Sdílí ji kolektor (stav k settle) i API
    (živý stav pro chip „dnes odpadá X %").
    """
    session_key = session_date.strftime("%Y%m%d")
    base = data_dir / "derived" / symbol
    out: list[ExpiryAtSettle] = []
    if not base.exists():
        return out
    for expiry_dir in sorted(base.iterdir()):
        expiry = expiry_dir.name
        if not (expiry.isdigit() and len(expiry) == 8 and expiry >= session_key):
            continue
        path = expiry_dir / "levels" / f"{session_date.isoformat()}.parquet"
        if not path.exists():
            continue
        try:
            table = pq.read_table(
                path, columns=["ts_min", "total_gex", "flip", "call_wall", "put_wall"]
            )
        except Exception:
            logger.exception("Levels partice %s nečitelná — expirace se přeskočí", path)
            continue
        last: dict[str, object] | None = None
        for record in table.to_pylist():
            ts = record["ts_min"]
            if ts is None or ts > at_ts:
                continue
            if last is None or ts >= last["ts_min"]:
                last = record
        if last is None or last["total_gex"] is None:
            continue
        out.append(
            ExpiryAtSettle(
                expiry=expiry,
                total_gex=float(last["total_gex"]),  # type: ignore[arg-type]
                flip=float(last["flip"]) if last["flip"] is not None else None,  # type: ignore[arg-type]
                call_wall=float(last["call_wall"]) if last["call_wall"] is not None else None,  # type: ignore[arg-type]
                put_wall=float(last["put_wall"]) if last["put_wall"] is not None else None,  # type: ignore[arg-type]
            )
        )
    return out


def _levels_days(data_dir: Path, symbol: str) -> list[dt.date]:
    """Dny, pro které existuje levels partice settlující expirace (backfill)."""
    base = data_dir / "derived" / symbol
    days: list[dt.date] = []
    if not base.exists():
        return days
    for expiry_dir in base.iterdir():
        expiry = expiry_dir.name
        if not (expiry.isdigit() and len(expiry) == 8):
            continue
        day = dt.date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:8]))
        if (expiry_dir / "levels" / f"{day.isoformat()}.parquet").exists():
            days.append(day)
    return sorted(days)


def session_ranges(data_dir: Path, symbol: str) -> list[tuple[dt.date, float]]:
    """(seance, high−low) z bars partic; bary se řadí seanci dle ADR-0023.

    Bary po settle seance se nepočítají — rozsah odpovídá „do settle",
    konzistentně s denní metrikou útesu.
    """
    bars_dir = data_dir / "derived" / symbol / "bars"
    if not bars_dir.exists():
        return []
    highs: dict[dt.date, float] = {}
    lows: dict[dt.date, float] = {}
    settles: dict[dt.date, dt.datetime] = {}
    for path in sorted(bars_dir.glob("*.parquet")):
        try:
            table = pq.read_table(path, columns=["ts_min", "high", "low"])
        except Exception:
            logger.exception("Bars partice %s nečitelná — přeskočena", path)
            continue
        for record in table.to_pylist():
            ts = record["ts_min"]
            if ts is None:
                continue
            session = trading_session_date(ts)
            boundary = settles.setdefault(session, settle_ts(session))
            if ts > boundary:
                continue
            highs[session] = max(highs.get(session, float(record["high"])), float(record["high"]))
            lows[session] = min(lows.get(session, float(record["low"])), float(record["low"]))
    return [(day, highs[day] - lows[day]) for day in sorted(highs)]


@dataclass
class GammaCliffCollector:
    """Jednou po settle spočítá záznam seance; průběžně dopočítává následující den."""

    symbol: str
    repository: GammaCliffRepository
    db: Engine
    data_dir: Path
    backfill: bool = True

    _evaluated_for: dt.date | None = field(default=None, init=False)
    _backfilled: bool = field(default=False, init=False)

    async def on_minute(self, now: dt.datetime) -> None:
        session = trading_session_date(now)
        boundary = settle_ts(session) + dt.timedelta(minutes=SETTLE_GRACE_MINUTES)
        if now < boundary or self._evaluated_for == session:
            return
        self._evaluated_for = session  # jeden pokus per seance i při chybě
        await asyncio.to_thread(self._run, session, now)

    def _run(self, session: dt.date, now: dt.datetime) -> None:
        if self.backfill and not self._backfilled:
            self._backfilled = True
            self._run_backfill(now)
        record = self._build_for(session)
        if record is not None:
            self.repository.upsert(record, now)
            logger.info(
                "%s: gamma útes %s — odpadá %.0f %% gammy%s",
                self.symbol,
                session.isoformat(),
                (record.cliff_share or 0.0) * 100,
                " (OPEX)" if record.is_opex else "",
            )
        self._fill_next_metrics(now)

    def _build_for(self, session: dt.date) -> CliffRecord | None:
        expiries = read_expiries_at(self.data_dir, self.symbol, session, settle_ts(session))
        return build_cliff(session, self.symbol, expiries)

    def _run_backfill(self, now: dt.datetime) -> None:
        existing = self.repository.existing_dates(self.symbol)
        written = 0
        for day in _levels_days(self.data_dir, self.symbol):
            if day in existing or settle_ts(day) > now:
                continue
            record = self._build_for(day)
            if record is None:
                continue
            self.repository.upsert(record, now)
            written += 1
        if written:
            logger.info(
                "%s: gamma útes backfill — %d seancí z levels historie", self.symbol, written
            )

    def _fill_next_metrics(self, now: dt.datetime) -> None:
        pending = self.repository.missing_next_metrics(self.symbol)
        if not pending:
            return
        ranges = session_ranges(self.data_dir, self.symbol)
        by_index = {day: index for index, (day, _) in enumerate(ranges)}
        for session in pending:
            # Následující seance = první s bary po dni záznamu; musí být settled
            following = next(((d, r) for d, r in ranges if d > session), None)
            if following is None or settle_ts(following[0]) > now:
                continue
            next_day, next_range = following
            index = by_index[next_day]
            previous = [value for _, value in ranges[:index]]
            self.repository.update_next_metrics(
                session,
                self.symbol,
                next_range_atr=range_in_atr(next_range, previous),
                next_setups=self._setup_stats(next_day),
            )

    def _setup_stats(self, session: dt.date) -> dict[str, dict[str, float]] | None:
        """Setupy následující seance per šablona: počet, uzavřené, Σ R, výhry."""
        start, end = session_bounds(session)
        stmt = select(
            setups_table.c.template, setups_table.c.outcome_r, setups_table.c.status
        ).where(
            setups_table.c.symbol == self.symbol,
            setups_table.c.created_ts >= start,
            setups_table.c.created_ts < end,
        )
        with self.db.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        if not rows:
            return None
        stats: dict[str, dict[str, float]] = {}
        for row in rows:
            bucket = stats.setdefault(
                str(row.template), {"count": 0, "closed": 0, "sum_r": 0.0, "wins": 0}
            )
            bucket["count"] += 1
            if row.outcome_r is not None:
                bucket["closed"] += 1
                bucket["sum_r"] += float(row.outcome_r)
                if float(row.outcome_r) > 0:
                    bucket["wins"] += 1
        return stats
