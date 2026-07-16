"""OI archiv (SPEC 3.5 + R4): EOD/ranní snapshot Open Interest do PostgreSQL, navždy.

Tabulka `oi_eod` se NIKDY nemaže (R4) — repository záměrně nenabízí žádné delete
API a RetentionJob (issue #12) se jí nesmí dotknout. Zápis je idempotentní upsert
přes primární klíč (symbol, expiry, strike, right, date).
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import Column, Date, Float, MetaData, String, Table, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.sql import Executable

from gexlens_engine.config import Settings
from gexlens_engine.ibkr.discovery import OptionContractSpec

logger = logging.getLogger(__name__)

metadata = MetaData()

oi_eod_table = Table(
    "oi_eod",
    metadata,
    Column("symbol", String(16), primary_key=True),
    Column("expiry", String(8), primary_key=True),
    Column("strike", Float, primary_key=True),
    Column("right", String(1), primary_key=True),
    Column("date", Date, primary_key=True),
    Column("oi", Float, nullable=False),
)


@dataclass(frozen=True)
class OIRecord:
    symbol: str
    expiry: str
    strike: float
    right: str
    day: dt.date
    oi: float


@dataclass(frozen=True)
class ArchiveResult:
    """Výsledek denní archivace: kolik záznamů zapsáno a které kontrakty OI nedodaly."""

    written: int
    missing: tuple[OptionContractSpec, ...]


class OIFetcherLike(Protocol):
    """Zdroj OI hodnoty kontraktu.

    Produkční implementace: FOP generic tick 588, akciové opce tick 101,
    fallback ranní reqMktData snapshot (ADR-0001: 588 intraday nechodí).
    Vrací None, když OI není k dispozici.
    """

    async def fetch_oi(self, spec: OptionContractSpec, timeout_s: float) -> float | None: ...


class OIEodRepository:
    """Přístup k tabulce oi_eod. Záměrně bez delete API (R4)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        metadata.create_all(self._engine)

    def upsert_many(self, records: Sequence[OIRecord]) -> None:
        """Idempotentní zápis: opakovaný běh týž den aktualizuje hodnoty (upsert)."""
        if not records:
            return
        rows = [
            {
                "symbol": r.symbol,
                "expiry": r.expiry,
                "strike": r.strike,
                "right": r.right,
                "date": r.day,
                "oi": r.oi,
            }
            for r in records
        ]
        dialect = self._engine.dialect.name
        primary_key = ["symbol", "expiry", "strike", "right", "date"]
        stmt: Executable
        if dialect == "postgresql":
            pg_stmt = pg_insert(oi_eod_table).values(rows)
            stmt = pg_stmt.on_conflict_do_update(
                index_elements=primary_key, set_={"oi": pg_stmt.excluded.oi}
            )
        elif dialect == "sqlite":
            sqlite_stmt = sqlite_insert(oi_eod_table).values(rows)
            stmt = sqlite_stmt.on_conflict_do_update(
                index_elements=primary_key, set_={"oi": sqlite_stmt.excluded.oi}
            )
        else:
            raise ValueError(f"Nepodporovaný databázový dialekt pro upsert: {dialect!r}")
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def days(self, symbol: str) -> list[dt.date]:
        stmt = (
            select(oi_eod_table.c.date)
            .where(oi_eod_table.c.symbol == symbol)
            .distinct()
            .order_by(oi_eod_table.c.date)
        )
        with self._engine.connect() as conn:
            return [row.date for row in conn.execute(stmt)]

    def count_for_day(self, symbol: str, day: dt.date) -> int:
        stmt = (
            select(func.count())
            .select_from(oi_eod_table)
            .where(oi_eod_table.c.symbol == symbol, oi_eod_table.c.date == day)
        )
        with self._engine.connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    def get_oi(self, symbol: str, day: dt.date, strike: float, right: str) -> float | None:
        stmt = select(oi_eod_table.c.oi).where(
            oi_eod_table.c.symbol == symbol,
            oi_eod_table.c.date == day,
            oi_eod_table.c.strike == strike,
            oi_eod_table.c.right == right,
        )
        with self._engine.connect() as conn:
            result = conn.execute(stmt).scalar_one_or_none()
            return float(result) if result is not None else None


class OIArchiver:
    """Denní archivace OI celého řetězce do oi_eod (bez retence, R4)."""

    def __init__(
        self, repository: OIEodRepository, fetcher: OIFetcherLike, settings: Settings
    ) -> None:
        self._repository = repository
        self._fetcher = fetcher
        self._settings = settings

    async def archive_day(
        self, contracts: Sequence[OptionContractSpec], day: dt.date
    ) -> ArchiveResult:
        """Stáhne OI všech kontraktů (po dávkách) a idempotentně zapíše do DB."""
        records: list[OIRecord] = []
        missing: list[OptionContractSpec] = []
        batch_size = self._settings.batch_size
        for offset in range(0, len(contracts), batch_size):
            batch = contracts[offset : offset + batch_size]
            values = await asyncio.gather(*(self._fetch_one(spec) for spec in batch))
            for spec, oi in zip(batch, values, strict=True):
                if oi is None:
                    missing.append(spec)
                else:
                    records.append(
                        OIRecord(
                            symbol=spec.symbol,
                            expiry=spec.expiry,
                            strike=spec.strike,
                            right=spec.right,
                            day=day,
                            oi=oi,
                        )
                    )
        await asyncio.to_thread(self._repository.upsert_many, records)
        if missing:
            logger.warning("OI archivace %s: %d kontraktů bez OI", day, len(missing))
        return ArchiveResult(written=len(records), missing=tuple(missing))

    async def _fetch_one(self, spec: OptionContractSpec) -> float | None:
        try:
            return await self._fetcher.fetch_oi(spec, self._settings.batch_timeout_s)
        except Exception:
            logger.exception("fetch_oi selhal pro %s", spec)
            return None
