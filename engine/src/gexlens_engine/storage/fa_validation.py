"""Úložiště a noční job denní FA validace (#232, ADR-0011).

Job běží v pipeline po úspěšném ranním OI archivu: pro každou expiraci se
snapshotem z předchozího archivního dne spočítá open-ratio a rank korelaci
(`compute.favalidation`) a bod uloží do tabulky `fa_validation`. Kalibrace α
pak čte hotové body přímo z PG — žádné ruční spouštění validačních skriptů.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq
from sqlalchemy import Column, Date, Float, Integer, MetaData, String, Table, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.sql import Executable

from gexlens_engine.compute.favalidation import (
    FaValidationPoint,
    Key,
    compute_fa_validation,
)
from gexlens_engine.storage.oi_archive import OIEodRepository

logger = logging.getLogger(__name__)

metadata = MetaData()

fa_validation_table = Table(
    "fa_validation",
    metadata,
    Column("symbol", String(16), primary_key=True),
    Column("expiry", String(16), primary_key=True),
    Column("day", Date, primary_key=True),
    Column("next_day", Date, nullable=False),
    Column("contracts", Integer, nullable=False),
    Column("volume_sum", Float, nullable=False),
    Column("doi_abs_sum", Float, nullable=False),
    Column("doi_net_sum", Float, nullable=False),
    Column("open_ratio", Float, nullable=False),
    Column("spearman", Float, nullable=False),
    Column("silent_share", Float, nullable=False),
)

# Řez trade date: volume counter IBKR se resetuje ve 22:00 UTC (start nové
# seance), poslední poctivá kumulativní hodnota dne je před 21:00 UTC (#232)
CUTOFF_HOUR_UTC = 21


@dataclass(frozen=True)
class FaValidationRecord:
    symbol: str
    expiry: str
    day: dt.date  # den, jehož volume se validuje
    next_day: dt.date  # den archivu, vůči němuž se počítá ΔOI
    point: FaValidationPoint


class FaValidationRepository:
    """Přístup k tabulce fa_validation; upsert je idempotentní vůči restartům."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        metadata.create_all(self._engine)

    def exists(self, symbol: str, expiry: str, day: dt.date) -> bool:
        stmt = select(fa_validation_table.c.symbol).where(
            fa_validation_table.c.symbol == symbol,
            fa_validation_table.c.expiry == expiry,
            fa_validation_table.c.day == day,
        )
        with self._engine.connect() as conn:
            return conn.execute(stmt).first() is not None

    def upsert(self, record: FaValidationRecord) -> None:
        row = {
            "symbol": record.symbol,
            "expiry": record.expiry,
            "day": record.day,
            "next_day": record.next_day,
            "contracts": record.point.contracts,
            "volume_sum": record.point.volume_sum,
            "doi_abs_sum": record.point.doi_abs_sum,
            "doi_net_sum": record.point.doi_net_sum,
            "open_ratio": record.point.open_ratio,
            "spearman": record.point.spearman,
            "silent_share": record.point.silent_share,
        }
        primary_key = ["symbol", "expiry", "day"]
        update_cols = {k: v for k, v in row.items() if k not in primary_key}
        dialect = self._engine.dialect.name
        stmt: Executable
        if dialect == "postgresql":
            pg_stmt = pg_insert(fa_validation_table).values([row])
            stmt = pg_stmt.on_conflict_do_update(
                index_elements=primary_key,
                set_={k: pg_stmt.excluded[k] for k in update_cols},
            )
        elif dialect == "sqlite":
            sqlite_stmt = sqlite_insert(fa_validation_table).values([row])
            stmt = sqlite_stmt.on_conflict_do_update(
                index_elements=primary_key,
                set_={k: sqlite_stmt.excluded[k] for k in update_cols},
            )
        else:
            raise ValueError(f"Nepodporovaný databázový dialekt pro upsert: {dialect!r}")
        with self._engine.begin() as conn:
            conn.execute(stmt)


def volumes_at_cutoff(path: Path, day: dt.date) -> dict[Key, float]:
    """Poslední kumulativní volume každé strany (strike, right) k řezu 21:00 UTC."""
    cutoff = dt.datetime.combine(day, dt.time(CUTOFF_HOUR_UTC), tzinfo=dt.UTC)
    table = pq.read_table(path, columns=["ts_min", "strike", "right", "volume"])
    latest: dict[Key, tuple[dt.datetime, float]] = {}
    columns = [table.column(name).to_pylist() for name in ("ts_min", "strike", "right", "volume")]
    for ts, strike, right, volume in zip(*columns, strict=True):
        if ts is None or ts > cutoff:
            continue
        key = (float(strike), str(right))
        current = latest.get(key)
        if current is None or ts >= current[0]:
            latest[key] = (ts, float(volume) if volume is not None else 0.0)
    return {key: volume for key, (_, volume) in latest.items()}


def collect_fa_validation(
    symbol: str,
    snapshots_dir: Path,
    oi_repository: OIEodRepository,
    fa_repository: FaValidationRepository,
    today: dt.date,
) -> list[FaValidationRecord]:
    """Spočítá a uloží chybějící validační body symbolu k dnešnímu OI archivu.

    Pro každou expiraci se snapshotem z posledního archivního dne < today,
    která má OI v obou dnech (mrtvé expirace dnešní OI nemají a přeskočí se),
    vrátí nově uložené záznamy. Blokující (parquet + DB) — volat přes to_thread.
    """
    records: list[FaValidationRecord] = []
    base = snapshots_dir / symbol
    if not base.is_dir():
        return records
    for exp_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        expiry = exp_dir.name
        previous = oi_repository.latest_day_before(symbol, expiry, today)
        if previous is None or fa_repository.exists(symbol, expiry, previous):
            continue
        snapshot_path = exp_dir / f"{previous.isoformat()}.parquet"
        if not snapshot_path.exists():
            continue
        oi_after = {
            (r.strike, r.right): r.oi for r in oi_repository.values_for(symbol, expiry, today)
        }
        if not oi_after:
            continue
        oi_before = {
            (r.strike, r.right): r.oi for r in oi_repository.values_for(symbol, expiry, previous)
        }
        volumes = volumes_at_cutoff(snapshot_path, previous)
        point = compute_fa_validation(volumes, oi_before, oi_after)
        if point is None:
            logger.info(
                "FA validace %s %s %s: nedostatečný vzorek — bod se neukládá",
                symbol,
                expiry,
                previous,
            )
            continue
        record = FaValidationRecord(symbol, expiry, previous, today, point)
        fa_repository.upsert(record)
        records.append(record)
    return records
