"""Trvalé úložiště setupů (ADR-0004): historie analýz pro kalibraci.

Tabulka záměrně nemá delete API (jako oi_eod) — výsledky setupů jsou dataset,
ze kterého se časem kalibruje confidence. Jediná mutace po uzavření je ruční
hodnocení uživatele (rating + poznámka).
"""

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

setups_metadata = MetaData()

setups_table = Table(
    "setups",
    setups_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String(16), nullable=False),
    Column("expiry", String(8), nullable=False),
    Column("template", String(32), nullable=False),
    Column("direction", String(8), nullable=False),
    Column("created_ts", DateTime(timezone=True), nullable=False),
    Column("entry", Float, nullable=False),
    Column("target", Float, nullable=False),
    Column("stop", Float, nullable=False),
    Column("confidence", Integer, nullable=False),
    Column("reason", Text, nullable=False),
    Column("context", JSON, nullable=False, default=dict),
    Column("status", String(16), nullable=False, default="active"),
    Column("closed_ts", DateTime(timezone=True), nullable=True),
    Column("outcome_r", Float, nullable=True),
    Column("mfe", Float, nullable=True),
    Column("mae", Float, nullable=True),
    Column("user_rating", Integer, nullable=True),  # null / +1 / −1
    Column("user_note", Text, nullable=True),
)


@dataclass(frozen=True)
class StoredSetup:
    id: int
    symbol: str
    expiry: str
    template: str
    direction: str
    created_ts: dt.datetime
    entry: float
    target: float
    stop: float
    confidence: int
    reason: str
    status: str


class SetupsRepository:
    """CRUD nad setups (bez delete — R4 duch platí i tady)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        setups_metadata.create_all(self._engine)

    def create(
        self,
        *,
        symbol: str,
        expiry: str,
        template: str,
        direction: str,
        created_ts: dt.datetime,
        entry: float,
        target: float,
        stop: float,
        confidence: int,
        reason: str,
        context: dict[str, Any],
    ) -> int:
        stmt = insert(setups_table).values(
            symbol=symbol,
            expiry=expiry,
            template=template,
            direction=direction,
            created_ts=created_ts,
            entry=entry,
            target=target,
            stop=stop,
            confidence=confidence,
            reason=reason,
            context=json.loads(json.dumps(context, default=str)),
            status="active",
        )
        with self._engine.begin() as conn:
            result = conn.execute(stmt)
        key = result.inserted_primary_key
        if key is None:
            raise RuntimeError("Insert setupu nevrátil primární klíč")
        return int(key[0])

    def close(
        self,
        setup_id: int,
        *,
        status: str,
        closed_ts: dt.datetime,
        outcome_r: float,
        mfe: float,
        mae: float,
    ) -> None:
        stmt = (
            update(setups_table)
            .where(setups_table.c.id == setup_id)
            .values(status=status, closed_ts=closed_ts, outcome_r=outcome_r, mfe=mfe, mae=mae)
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def review(self, setup_id: int, rating: int | None, note: str | None) -> bool:
        stmt = (
            update(setups_table)
            .where(setups_table.c.id == setup_id)
            .values(user_rating=rating, user_note=note)
        )
        with self._engine.begin() as conn:
            result = conn.execute(stmt)
        return result.rowcount > 0

    def active_for(self, symbol: str) -> list[StoredSetup]:
        stmt = select(setups_table).where(
            setups_table.c.symbol == symbol, setups_table.c.status == "active"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [
            StoredSetup(
                id=row.id,
                symbol=row.symbol,
                expiry=row.expiry,
                template=row.template,
                direction=row.direction,
                created_ts=row.created_ts,
                entry=row.entry,
                target=row.target,
                stop=row.stop,
                confidence=row.confidence,
                reason=row.reason,
                status=row.status,
            )
            for row in rows
        ]

    def list_for(
        self,
        symbol: str,
        *,
        date: dt.date | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        stmt = select(setups_table).where(setups_table.c.symbol == symbol)
        if status is not None:
            stmt = stmt.where(setups_table.c.status == status)
        if date is not None:
            start = dt.datetime.combine(date, dt.time.min, tzinfo=dt.UTC)
            stmt = stmt.where(
                setups_table.c.created_ts >= start,
                setups_table.c.created_ts < start + dt.timedelta(days=1),
            )
        stmt = stmt.order_by(setups_table.c.created_ts.desc()).limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row._mapping)
            for key in ("created_ts", "closed_ts"):
                value = record.get(key)
                if isinstance(value, dt.datetime):
                    record[key] = value.isoformat()
            result.append(record)
        return result
