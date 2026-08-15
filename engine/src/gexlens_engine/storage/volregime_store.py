"""Tabulka `vol_regime` (ADR-0028, #713) — po vzoru `gamma_cliff`.

Denní volatilitní režim per (seance, symbol). PostgreSQL navždy: metrika stojí
na barech, které se také nikdy nemažou, takže historie roste souvisle a
percentily se s časem zpřesňují.

Ukládá se HODNOTA i verze definice, ne jen kategorie — hranice se budou
kalibrovat a zpětné přeřazení starých záznamů by falšovalo, podle čeho se
tehdy rozhodovalo.
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

from gexlens_engine.compute.volregime import VolRegime

vol_regime_metadata = MetaData()

vol_regime_table = Table(
    "vol_regime",
    vol_regime_metadata,
    Column("session_date", Date, primary_key=True),
    Column("symbol", String(16), primary_key=True),
    Column("session_range", Float, nullable=False),
    Column("percentile", Float, nullable=False),
    Column("bucket", String(16), nullable=False),
    Column("sample", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False),
)


class VolRegimeRepository:
    """Upsert per (session_date, symbol) — idempotentní vůči restartu i backfillu."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        vol_regime_metadata.create_all(self._engine)

    def existing_dates(self, symbol: str) -> set[dt.date]:
        stmt = select(vol_regime_table.c.session_date).where(vol_regime_table.c.symbol == symbol)
        with self._engine.connect() as conn:
            return {row.session_date for row in conn.execute(stmt)}

    def upsert(self, record: VolRegime, computed_at: dt.datetime) -> None:
        values = {
            "session_date": record.session_date,
            "symbol": record.symbol,
            "session_range": record.session_range,
            "percentile": record.percentile,
            "bucket": record.bucket,
            "sample": record.sample,
            "version": record.version,
            "computed_at": computed_at,
        }
        with self._engine.begin() as conn:
            updated = conn.execute(
                update(vol_regime_table)
                .where(
                    vol_regime_table.c.session_date == record.session_date,
                    vol_regime_table.c.symbol == record.symbol,
                )
                .values(**values)
            )
            if updated.rowcount == 0:
                conn.execute(vol_regime_table.insert().values(**values))

    def list_for(self, symbol: str, *, limit: int = 365) -> list[dict[str, object]]:
        stmt = (
            select(vol_regime_table)
            .where(vol_regime_table.c.symbol == symbol)
            .order_by(vol_regime_table.c.session_date.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = [dict(row._mapping) for row in conn.execute(stmt)]
        for row in rows:
            session_date = row.get("session_date")
            if isinstance(session_date, dt.date):
                row["session_date"] = session_date.isoformat()
            computed = row.get("computed_at")
            if isinstance(computed, dt.datetime):
                row["computed_at"] = computed.isoformat()
        return rows

    def for_session(self, symbol: str, session_date: dt.date) -> dict[str, object] | None:
        stmt = select(vol_regime_table).where(
            vol_regime_table.c.symbol == symbol,
            vol_regime_table.c.session_date == session_date,
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None:
            return None
        record = dict(row._mapping)
        if isinstance(record.get("session_date"), dt.date):
            record["session_date"] = record["session_date"].isoformat()
        if isinstance(record.get("computed_at"), dt.datetime):
            record["computed_at"] = record["computed_at"].isoformat()
        return record
