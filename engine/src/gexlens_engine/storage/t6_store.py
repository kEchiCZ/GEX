"""Kandidátní dny T6 „Premarket squeeze" (#256) — vlastní metadata po vzoru
`tendency_store`. Sběr statistiky pro budoucí rozhodnutí o šabloně; ruční
verdikt (konal se squeeze?) se zapisuje u issue, ne sem.
"""

import datetime as dt
from typing import Any

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    inspect,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.sql import Executable

# Verze konvence řezání dne (obdoba SETUP_MECHANICS_VERSION, #311):
# 1 = close-to-close řezané UTC půlnocí (do #498),
# 2 = den končí settle US seance (compute.settle, #498).
# Startovní přepočet (t6.recompute_stale_candidates) dorovná starší řádky.
# Pozn. #511: settle se odvozuje z burzovní timezone (16:00 ET) — v letním čase
# je hranice shodná s dřívějšími 20:00 UTC a všichni existující kandidáti jsou
# letní, výsledky se tedy nemění a verze zůstává 2. Zvednout až při změně,
# která reálně přepisuje existující data.
T6_CONVENTION_VERSION = 2

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
    Column("convention_version", Integer, nullable=False),
)


class T6Repository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        t6_metadata.create_all(self._engine)
        self._ensure_convention_version()

    def _ensure_convention_version(self) -> None:
        """Doplní `convention_version` do tabulky založené před #498.

        `create_all` existující tabulku nemění. Staré řádky dostanou verzi 1
        (UTC půlnoc) — startovní přepočet je pak najde a dorovná na settle
        konvenci. ALTER je idempotentní přes kontrolu inspektorem (vzor #311).
        """
        inspector = inspect(self._engine)
        if not inspector.has_table(t6_occurrences.name):
            return
        columns = {col["name"] for col in inspector.get_columns(t6_occurrences.name)}
        if "convention_version" in columns:
            return
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    f"ALTER TABLE {t6_occurrences.name} "
                    "ADD COLUMN convention_version INTEGER NOT NULL DEFAULT 1"
                )
            )

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
            "convention_version": T6_CONVENTION_VERSION,
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

    def list_stale(self, current_version: int) -> list[dict[str, Any]]:
        """Kandidáti uložení starší konvencí než `current_version` (všechny symboly)."""
        stmt = (
            select(t6_occurrences)
            .where(t6_occurrences.c.convention_version < current_version)
            .order_by(t6_occurrences.c.symbol, t6_occurrences.c.day)
        )
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def delete(self, *, symbol: str, day: dt.date) -> None:
        """Odstraní kandidáta — jen pro přepočet #498, když chybí bary pro přepočet."""
        stmt = t6_occurrences.delete().where(
            (t6_occurrences.c.symbol == symbol) & (t6_occurrences.c.day == day)
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)
