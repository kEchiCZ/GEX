"""Tabulka `map_state` (#1245) — po vzoru `vol_regime`.

Agregát stavu mapy per (seance, symbol): medián a p10 |total_gex|, medián
gammy u ceny a dominance zdí v US RTH, podíl minut ve stavu tenká mapa.
PostgreSQL navždy: `derived/` partice retenci mají, klouzavé okno 20 seancí
proto potřebuje trvalý řádek — a historie prahů je zároveň to, podle čeho
se bude definice kalibrovat (proto i verze).
"""

import datetime as dt

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    select,
    update,
)
from sqlalchemy.engine import Engine

from gexlens_engine.compute.mapstate import SessionMapStats

map_state_metadata = MetaData()

map_state_table = Table(
    "map_state",
    map_state_metadata,
    Column("session_date", Date, primary_key=True),
    Column("symbol", String(16), primary_key=True),
    Column("gex_abs_median", Float, nullable=False),
    Column("gex_abs_p10", Float, nullable=False),
    Column("gamma_abs_median", Float, nullable=True),
    Column("dom_median", Float, nullable=True),
    Column("thin_share", Float, nullable=True),
    Column("sample_minutes", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False),
)


class MapStateRepository:
    """Upsert per (session_date, symbol) — idempotentní vůči restartu i backfillu."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        map_state_metadata.create_all(self._engine)

    def existing_dates(self, symbol: str) -> set[dt.date]:
        stmt = select(map_state_table.c.session_date).where(map_state_table.c.symbol == symbol)
        with self._engine.connect() as conn:
            return {row.session_date for row in conn.execute(stmt)}

    def upsert(self, record: SessionMapStats, computed_at: dt.datetime) -> None:
        values = {
            "session_date": record.session_date,
            "symbol": record.symbol,
            "gex_abs_median": record.gex_abs_median,
            "gex_abs_p10": record.gex_abs_p10,
            "gamma_abs_median": record.gamma_abs_median,
            "dom_median": record.dom_median,
            "thin_share": record.thin_share,
            "sample_minutes": record.sample_minutes,
            "version": record.version,
            "computed_at": computed_at,
        }
        with self._engine.begin() as conn:
            updated = conn.execute(
                update(map_state_table)
                .where(
                    map_state_table.c.session_date == record.session_date,
                    map_state_table.c.symbol == record.symbol,
                )
                .values(**values)
            )
            if updated.rowcount == 0:
                conn.execute(map_state_table.insert().values(**values))

    def history_before(
        self, symbol: str, session_date: dt.date, *, window: int
    ) -> list[SessionMapStats]:
        """Posledních `window` seancí PŘED `session_date` (look-ahead vyloučen)."""
        stmt = (
            select(map_state_table)
            .where(
                map_state_table.c.symbol == symbol,
                map_state_table.c.session_date < session_date,
            )
            .order_by(map_state_table.c.session_date.desc())
            .limit(window)
        )
        with self._engine.connect() as conn:
            rows = [dict(row._mapping) for row in conn.execute(stmt)]
        return [
            SessionMapStats(
                session_date=row["session_date"],
                symbol=row["symbol"],
                gex_abs_median=float(row["gex_abs_median"]),
                gex_abs_p10=float(row["gex_abs_p10"]),
                gamma_abs_median=(
                    float(row["gamma_abs_median"]) if row["gamma_abs_median"] is not None else None
                ),
                dom_median=float(row["dom_median"]) if row["dom_median"] is not None else None,
                thin_share=float(row["thin_share"]) if row["thin_share"] is not None else None,
                sample_minutes=int(row["sample_minutes"]),
                version=int(row["version"]),
            )
            for row in rows
        ]

    def list_for(self, symbol: str, *, limit: int = 60) -> list[dict[str, object]]:
        stmt = (
            select(map_state_table)
            .where(map_state_table.c.symbol == symbol)
            .order_by(map_state_table.c.session_date.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = [dict(row._mapping) for row in conn.execute(stmt)]
        for row in rows:
            if isinstance(row.get("session_date"), dt.date):
                row["session_date"] = row["session_date"].isoformat()
            if isinstance(row.get("computed_at"), dt.datetime):
                row["computed_at"] = row["computed_at"].isoformat()
        return rows
