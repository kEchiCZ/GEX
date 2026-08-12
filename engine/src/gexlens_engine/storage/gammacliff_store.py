"""Tabulka `gamma_cliff` (#576, fáze 1) — vlastní metadata po vzoru tendency.

Denní záznam odpadu gammy per (seance, symbol) + metriky následující seance
(dopočítávané o den později). PostgreSQL navždy — fáze 2 kalibruje z historie.
`next_outside_share` (podíl minut `band_regime = outside`) se začne plnit až
s #575; sloupec existuje od začátku, ať se schéma nemusí měnit.
"""

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    MetaData,
    String,
    Table,
    select,
    update,
)
from sqlalchemy.engine import Engine

from gexlens_engine.compute.gammacliff import CliffRecord

gamma_cliff_metadata = MetaData()

gamma_cliff_table = Table(
    "gamma_cliff",
    gamma_cliff_metadata,
    Column("session_date", Date, primary_key=True),
    Column("symbol", String(16), primary_key=True),
    Column("gex_before", Float, nullable=False),
    Column("gex_expiring", Float, nullable=False),
    Column("cliff_share", Float, nullable=True),
    Column("is_opex", Boolean, nullable=False),
    Column("flip_shift", Float, nullable=True),
    Column("call_wall_shift", Float, nullable=True),
    Column("put_wall_shift", Float, nullable=True),
    # Metriky NÁSLEDUJÍCÍ seance — dopočet po jejím settle
    Column("next_range_atr", Float, nullable=True),
    # {template: {count, sum_r, wins, closed}} — setupy následující seance
    Column("next_setups", JSON, nullable=True),
    # Podíl minut band_regime=outside následující seance — plní až #575
    Column("next_outside_share", Float, nullable=True),
    Column("computed_at", DateTime(timezone=True), nullable=False),
)


class GammaCliffRepository:
    """Upsert per (session_date, symbol) — idempotentní vůči restartu i backfillu."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        gamma_cliff_metadata.create_all(self._engine)

    def existing_dates(self, symbol: str) -> set[dt.date]:
        stmt = select(gamma_cliff_table.c.session_date).where(gamma_cliff_table.c.symbol == symbol)
        with self._engine.connect() as conn:
            return {row.session_date for row in conn.execute(stmt)}

    def upsert(self, record: CliffRecord, computed_at: dt.datetime) -> None:
        values = {
            "session_date": record.session_date,
            "symbol": record.symbol,
            "gex_before": record.gex_before,
            "gex_expiring": record.gex_expiring,
            "cliff_share": record.cliff_share,
            "is_opex": record.is_opex,
            "flip_shift": record.flip_shift,
            "call_wall_shift": record.call_wall_shift,
            "put_wall_shift": record.put_wall_shift,
            "computed_at": computed_at,
        }
        with self._engine.begin() as conn:
            updated = conn.execute(
                update(gamma_cliff_table)
                .where(
                    gamma_cliff_table.c.session_date == record.session_date,
                    gamma_cliff_table.c.symbol == record.symbol,
                )
                .values(**values)
            )
            if updated.rowcount == 0:
                conn.execute(gamma_cliff_table.insert().values(**values))

    def missing_next_metrics(self, symbol: str) -> list[dt.date]:
        """Seance bez dopočtených metrik následujícího dne, vzestupně."""
        stmt = (
            select(gamma_cliff_table.c.session_date)
            .where(
                gamma_cliff_table.c.symbol == symbol,
                gamma_cliff_table.c.next_range_atr.is_(None),
            )
            .order_by(gamma_cliff_table.c.session_date)
        )
        with self._engine.connect() as conn:
            return [row.session_date for row in conn.execute(stmt)]

    def update_next_metrics(
        self,
        session_date: dt.date,
        symbol: str,
        *,
        next_range_atr: float | None,
        next_setups: dict[str, dict[str, float]] | None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(gamma_cliff_table)
                .where(
                    gamma_cliff_table.c.session_date == session_date,
                    gamma_cliff_table.c.symbol == symbol,
                )
                .values(next_range_atr=next_range_atr, next_setups=next_setups)
            )
