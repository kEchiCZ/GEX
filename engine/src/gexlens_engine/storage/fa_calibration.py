"""Úložiště a ranní job kalibrace α (#232, ADR-0011 fáze 2).

Job běží v pipeline po úspěšném ranním OI archivu (hned za FA validací):
včerejší konec dne řady netflow (řez 21:00 UTC — konec trade date) se
porovná s ΔOI mezi archivy, medián poměrů dá denní bod a EMA přes dny
aktualizuje α symbolu. Historie bodů zůstává v `fa_alpha_history` pro audit
(vč. buy/sell mediánů — případná asymetrie se rozhodne až nad daty).
Aktuální α per symbol drží `fa_alpha`; čte ji engine (runtime.flow_alpha)
i API (/fa/alpha pro frontend badge).
"""

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq
from sqlalchemy import Column, Date, DateTime, Float, Integer, MetaData, String, Table, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.sql import Executable

from gexlens_engine.compute.facalibration import (
    AlphaCalibrationPoint,
    Key,
    calibrate_alpha,
    update_alpha,
)
from gexlens_engine.storage.fa_validation import CUTOFF_HOUR_UTC
from gexlens_engine.storage.oi_archive import OIEodRepository

logger = logging.getLogger(__name__)

metadata = MetaData()

# Aktuální kalibrovaná α per symbol + počet započtených validačních dnů
fa_alpha_table = Table(
    "fa_alpha",
    metadata,
    Column("symbol", String(16), primary_key=True),
    Column("alpha", Float, nullable=False),
    Column("days", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# Historie denních bodů pro audit — surové mediány bez EMA a bez clampu
fa_alpha_history_table = Table(
    "fa_alpha_history",
    metadata,
    Column("symbol", String(16), primary_key=True),
    Column("day", Date, primary_key=True),  # den, jehož netflow se kalibroval
    Column("expiry", String(16), nullable=False),
    Column("samples", Integer, nullable=False),
    Column("ratio_median", Float, nullable=False),
    Column("ratio_buy", Float, nullable=True),
    Column("ratio_sell", Float, nullable=True),
    Column("alpha_after", Float, nullable=False),
)


@dataclass(frozen=True)
class FaAlphaState:
    """Aktuální α symbolu pro engine i API."""

    symbol: str
    alpha: float
    days: int
    updated_at: dt.datetime


@dataclass(frozen=True)
class AlphaCalibrationResult:
    """Výsledek jednoho ranního kalibračního běhu (pro log a alert)."""

    symbol: str
    day: dt.date
    expiry: str
    point: AlphaCalibrationPoint
    alpha_after: float
    days: int


class FaAlphaRepository:
    """Přístup k tabulkám fa_alpha / fa_alpha_history; upserty jsou idempotentní."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        metadata.create_all(self._engine)

    def get(self, symbol: str) -> FaAlphaState | None:
        stmt = select(
            fa_alpha_table.c.alpha, fa_alpha_table.c.days, fa_alpha_table.c.updated_at
        ).where(fa_alpha_table.c.symbol == symbol)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None:
            return None
        return FaAlphaState(symbol=symbol, alpha=row[0], days=row[1], updated_at=row[2])

    def list_all(self) -> list[FaAlphaState]:
        stmt = select(
            fa_alpha_table.c.symbol,
            fa_alpha_table.c.alpha,
            fa_alpha_table.c.days,
            fa_alpha_table.c.updated_at,
        ).order_by(fa_alpha_table.c.symbol)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [
            FaAlphaState(symbol=row[0], alpha=row[1], days=row[2], updated_at=row[3])
            for row in rows
        ]

    def history_exists(self, symbol: str, day: dt.date) -> bool:
        stmt = select(fa_alpha_history_table.c.symbol).where(
            fa_alpha_history_table.c.symbol == symbol,
            fa_alpha_history_table.c.day == day,
        )
        with self._engine.connect() as conn:
            return conn.execute(stmt).first() is not None

    def record(
        self,
        symbol: str,
        day: dt.date,
        expiry: str,
        point: AlphaCalibrationPoint,
        alpha_after: float,
        days: int,
        now: dt.datetime | None = None,
    ) -> None:
        """Uloží denní bod do historie a přepíše aktuální α symbolu (transakčně)."""
        now = now or dt.datetime.now(dt.UTC)
        history_row = {
            "symbol": symbol,
            "day": day,
            "expiry": expiry,
            "samples": point.samples,
            "ratio_median": point.ratio_median,
            "ratio_buy": point.ratio_buy,
            "ratio_sell": point.ratio_sell,
            "alpha_after": alpha_after,
        }
        alpha_row = {"symbol": symbol, "alpha": alpha_after, "days": days, "updated_at": now}
        with self._engine.begin() as conn:
            conn.execute(self._upsert(fa_alpha_history_table, history_row, ["symbol", "day"]))
            conn.execute(self._upsert(fa_alpha_table, alpha_row, ["symbol"]))

    def _upsert(self, table: Table, row: dict[str, object], primary_key: list[str]) -> Executable:
        update_cols = {k: v for k, v in row.items() if k not in primary_key}
        dialect = self._engine.dialect.name
        if dialect == "postgresql":
            pg_stmt = pg_insert(table).values([row])
            return pg_stmt.on_conflict_do_update(
                index_elements=primary_key,
                set_={k: pg_stmt.excluded[k] for k in update_cols},
            )
        if dialect == "sqlite":
            sqlite_stmt = sqlite_insert(table).values([row])
            return sqlite_stmt.on_conflict_do_update(
                index_elements=primary_key,
                set_={k: sqlite_stmt.excluded[k] for k in update_cols},
            )
        raise ValueError(f"Nepodporovaný databázový dialekt pro upsert: {dialect!r}")


def netflow_at_cutoff(path: Path, day: dt.date) -> dict[Key, float]:
    """Poslední kumulativní net každé strany k řezu 21:00 UTC (konec trade date).

    Stejný řez jako FA validace: po 21:00 UTC začíná další seance a kumulativ
    už patří novému trade date (counter volume IBKR se resetuje ve 22:00 UTC).
    """
    cutoff = dt.datetime.combine(day, dt.time(CUTOFF_HOUR_UTC), tzinfo=dt.UTC)
    table = pq.read_table(path, columns=["ts_min", "strike", "right", "net_volume"])
    latest: dict[Key, tuple[dt.datetime, float]] = {}
    columns = [
        table.column(name).to_pylist() for name in ("ts_min", "strike", "right", "net_volume")
    ]
    for ts, strike, right, net in zip(*columns, strict=True):
        if ts is None or ts > cutoff:
            continue
        key = (float(strike), str(right))
        current = latest.get(key)
        if current is None or ts >= current[0]:
            latest[key] = (ts, float(net) if net is not None else 0.0)
    return {key: net for key, (_, net) in latest.items()}


def collect_alpha_calibration(
    symbol: str,
    derived_dir: Path,
    oi_repository: OIEodRepository,
    alpha_repository: FaAlphaRepository,
    today: dt.date,
) -> AlphaCalibrationResult | None:
    """Spočítá a uloží chybějící kalibrační bod symbolu k dnešnímu OI archivu.

    Netflow píše jen aktivní řetěz, takže den má nejvýš jednu expiraci s daty.
    Bere se poslední archivní den < today, který má netflow partici a OI v obou
    dnech; hotové dny přeskakuje (idempotentní dedup v historii). Blokující
    (parquet + DB) — volat přes to_thread.
    """
    base = derived_dir / symbol
    if not base.is_dir():
        return None
    for exp_dir in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True):
        expiry = exp_dir.name
        netflow_dir = exp_dir / "netflow"
        if not netflow_dir.is_dir():
            continue
        previous = oi_repository.latest_day_before(symbol, expiry, today)
        if previous is None or alpha_repository.history_exists(symbol, previous):
            continue
        netflow_path = netflow_dir / f"{previous.isoformat()}.parquet"
        if not netflow_path.exists():
            continue
        oi_after = {
            (r.strike, r.right): r.oi for r in oi_repository.values_for(symbol, expiry, today)
        }
        if not oi_after:
            continue  # mrtvá expirace — dnešní archiv ji nenese
        oi_before = {
            (r.strike, r.right): r.oi for r in oi_repository.values_for(symbol, expiry, previous)
        }
        netflow = netflow_at_cutoff(netflow_path, previous)
        doi = {
            key: float(oi_after.get(key, 0.0)) - float(oi_before.get(key, 0.0))
            for key in set(netflow) | set(oi_before) | set(oi_after)
        }
        point = calibrate_alpha(netflow, doi)
        if point is None:
            logger.info(
                "Kalibrace α %s %s %s: nedostatečný vzorek — bod se neukládá",
                symbol,
                expiry,
                previous,
            )
            continue
        state = alpha_repository.get(symbol)
        alpha_after = update_alpha(state.alpha if state else None, point.ratio_median)
        days = (state.days if state else 0) + 1
        alpha_repository.record(symbol, previous, expiry, point, alpha_after, days)
        return AlphaCalibrationResult(
            symbol=symbol,
            day=previous,
            expiry=expiry,
            point=point,
            alpha_after=alpha_after,
            days=days,
        )
    return None
