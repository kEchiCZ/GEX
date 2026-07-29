"""Minutová historie indikátoru tendence (#350) — vlastní metadata po vzoru
`setups_store` (nemíchat s UI metadaty, která vytváří i API proces).

Zapisuje se každá minuta včetně rozpadu hlasů a verze vah (S11): až se váhy
překalibrují, staré záznamy nesmí vypadat, že vznikly novým modelem. Bez
uložené historie by nešlo zpětně ověřit, jestli „Strong Long" opravdu
předchází růstu — a indikátor by byl jen dekorace.
"""

import datetime as dt
import json
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
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.sql import Executable

from gexlens_engine.compute.tendency import TendencyResult

tendency_metadata = MetaData()

tendency_table = Table(
    "tendency",
    tendency_metadata,
    Column("ts_min", DateTime(timezone=True), primary_key=True),
    Column("symbol", String(16), primary_key=True),
    Column("score", Float, nullable=False),
    Column("band", String(16), nullable=False),
    # Rozpad hlasů (name/vote/weight/detail) — „žádná černá skříňka"
    Column("votes", JSON, nullable=False),
    Column("weights_version", Integer, nullable=False),
)


def votes_payload(result: TendencyResult) -> list[dict[str, Any]]:
    """Hlasy jako JSON-serializovatelný rozpad pro DB i WS."""
    return [
        {"name": vote.name, "vote": vote.vote, "weight": vote.weight, "detail": vote.detail}
        for vote in result.votes
    ]


class TendencyRepository:
    """Upsert per (ts_min, symbol) — idempotentní vůči restartu enginu."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        tendency_metadata.create_all(self._engine)

    def upsert(self, symbol: str, result: TendencyResult) -> None:
        row = {
            "ts_min": result.ts_min,
            "symbol": symbol,
            "score": result.score,
            "band": result.band,
            "votes": json.loads(json.dumps(votes_payload(result), default=str)),
            "weights_version": result.weights_version,
        }
        primary_key = ["ts_min", "symbol"]
        update_cols = {k: v for k, v in row.items() if k not in primary_key}
        dialect = self._engine.dialect.name
        stmt: Executable
        if dialect == "postgresql":
            pg_stmt = pg_insert(tendency_table).values([row])
            stmt = pg_stmt.on_conflict_do_update(
                index_elements=primary_key,
                set_={k: pg_stmt.excluded[k] for k in update_cols},
            )
        elif dialect == "sqlite":
            sqlite_stmt = sqlite_insert(tendency_table).values([row])
            stmt = sqlite_stmt.on_conflict_do_update(
                index_elements=primary_key,
                set_={k: sqlite_stmt.excluded[k] for k in update_cols},
            )
        else:
            raise ValueError(f"Nepodporovaný databázový dialekt pro upsert: {dialect!r}")
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def series_for(self, symbol: str, day: dt.date) -> list[dict[str, Any]]:
        """Minutová řada dne — měřitelnost (#350 bod 2) a REST pro UI."""
        start = dt.datetime.combine(day, dt.time(), dt.UTC)
        stmt = (
            select(tendency_table)
            .where(
                tendency_table.c.symbol == symbol,
                tendency_table.c.ts_min >= start,
                tendency_table.c.ts_min < start + dt.timedelta(days=1),
            )
            .order_by(tendency_table.c.ts_min)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [dict(row._mapping) for row in rows]
