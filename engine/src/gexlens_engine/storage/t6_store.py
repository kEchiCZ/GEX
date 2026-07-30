"""Kandidátní dny T6 „Premarket squeeze" (#256) — vlastní metadata po vzoru
`tendency_store`. Sběr statistiky pro budoucí rozhodnutí o šabloně; ruční
verdikt (konal se squeeze?) se zapisuje u issue, ne sem.
"""

import datetime as dt
from typing import Any

from sqlalchemy import Column, Date, DateTime, Float, MetaData, String, Table, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.sql import Executable

t6_metadata = MetaData()

t6_occurrences = Table(
    "t6_occurrences",
    t6_metadata,
    Column("day", Date, primary_key=True),
    Column("symbol", String(16), primary_key=True),
    Column("trigger_close_pct", Float, nullable=False),
    Column("overnight_move_pct", Float, nullable=True),
    Column("put_oi_increase", Float, nullable=True),
    Column("gex_regime", String(16), nullable=True),
    Column("max_pain", Float, nullable=True),
    Column("spot", Float, nullable=False),
    Column("evaluated_at", DateTime(timezone=True), nullable=False),
)


class T6Repository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        t6_metadata.create_all(self._engine)

    def upsert(
        self,
        *,
        symbol: str,
        day: dt.date,
        trigger_close_pct: float,
        overnight_move_pct: float | None,
        put_oi_increase: float | None,
        gex_regime: str | None,
        max_pain: float | None,
        spot: float,
        evaluated_at: dt.datetime,
    ) -> None:
        row: dict[str, Any] = {
            "day": day,
            "symbol": symbol,
            "trigger_close_pct": trigger_close_pct,
            "overnight_move_pct": overnight_move_pct,
            "put_oi_increase": put_oi_increase,
            "gex_regime": gex_regime,
            "max_pain": max_pain,
            "spot": spot,
            "evaluated_at": evaluated_at,
        }
        primary_key = ["day", "symbol"]
        update_cols = {k: v for k, v in row.items() if k not in primary_key}
        dialect = self._engine.dialect.name
        stmt: Executable
        if dialect == "postgresql":
            pg_stmt = pg_insert(t6_occurrences).values([row])
            stmt = pg_stmt.on_conflict_do_update(
                index_elements=primary_key,
                set_={k: pg_stmt.excluded[k] for k in update_cols},
            )
        elif dialect == "sqlite":
            sqlite_stmt = sqlite_insert(t6_occurrences).values([row])
            stmt = sqlite_stmt.on_conflict_do_update(
                index_elements=primary_key,
                set_={k: sqlite_stmt.excluded[k] for k in update_cols},
            )
        else:
            raise ValueError(f"Nepodporovaný databázový dialekt pro upsert: {dialect!r}")
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def list_for(self, symbol: str) -> list[dict[str, Any]]:
        stmt = (
            select(t6_occurrences)
            .where(t6_occurrences.c.symbol == symbol)
            .order_by(t6_occurrences.c.day)
        )
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]
