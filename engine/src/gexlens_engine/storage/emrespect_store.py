"""Tabulka `em_respect` (#872) — po vzoru `vol_regime` (ADR-0028).

Respektování pásma expected move per (seance, symbol). PostgreSQL navždy:
kalibrace důvěry v EM potřebuje souvislou historii a řádek je pár set bajtů.
Ukládá se zdroj EM (straddle × close prémie) i verze definice — obě varianty
nejsou totéž číslo a budoucí rekalibrace nesmí přepsat, podle čeho se
klasifikovalo tehdy.
"""

import datetime as dt

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    func,
    select,
    update,
)
from sqlalchemy.engine import Engine

from gexlens_engine.compute.emrespect import EmRespect

em_respect_metadata = MetaData()

em_respect_table = Table(
    "em_respect",
    em_respect_metadata,
    Column("session_date", Date, primary_key=True),
    Column("symbol", String(16), primary_key=True),
    # Referenční bod pásma: NULL ts = fallback z close prémií (pre-open odhad)
    Column("ref_ts", DateTime(timezone=True), nullable=True),
    Column("em_source", String(12), nullable=False),
    Column("anchor", Float, nullable=False),
    Column("atm_strike", Float, nullable=False),
    Column("em_points", Float, nullable=False),
    Column("high", Float, nullable=False),
    Column("low", Float, nullable=False),
    Column("close", Float, nullable=False),
    Column("close_in_band", Boolean, nullable=False),
    Column("touch_upper", Boolean, nullable=False),
    Column("touch_lower", Boolean, nullable=False),
    Column("range_vs_em", Float, nullable=False),
    # Podíl minut v negativní gammě (#876); NULL = levels pro seanci nejsou
    Column("negative_gamma_share", Float, nullable=True),
    Column("version", Integer, nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False),
)


class EmRespectRepository:
    """Upsert per (session_date, symbol) — idempotentní vůči restartu i backfillu."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        em_respect_metadata.create_all(self._engine)

    def existing_dates(self, symbol: str) -> set[dt.date]:
        stmt = select(em_respect_table.c.session_date).where(em_respect_table.c.symbol == symbol)
        with self._engine.connect() as conn:
            return {row.session_date for row in conn.execute(stmt)}

    def upsert(self, record: EmRespect, computed_at: dt.datetime) -> None:
        values = {
            "session_date": record.session_date,
            "symbol": record.symbol,
            "ref_ts": record.reference.ts,
            "em_source": record.reference.source,
            "anchor": record.reference.anchor,
            "atm_strike": record.reference.atm_strike,
            "em_points": record.reference.em_points,
            "high": record.high,
            "low": record.low,
            "close": record.close,
            "close_in_band": record.close_in_band,
            "touch_upper": record.touch_upper,
            "touch_lower": record.touch_lower,
            "range_vs_em": record.range_vs_em,
            "negative_gamma_share": record.negative_gamma_share,
            "version": record.version,
            "computed_at": computed_at,
        }
        with self._engine.begin() as conn:
            updated = conn.execute(
                update(em_respect_table)
                .where(
                    em_respect_table.c.session_date == record.session_date,
                    em_respect_table.c.symbol == record.symbol,
                )
                .values(**values)
            )
            if updated.rowcount == 0:
                conn.execute(em_respect_table.insert().values(**values))

    def list_for(self, symbol: str, *, limit: int = 365) -> list[dict[str, object]]:
        stmt = (
            select(em_respect_table)
            .where(em_respect_table.c.symbol == symbol)
            .order_by(em_respect_table.c.session_date.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = [dict(row._mapping) for row in conn.execute(stmt)]
        for row in rows:
            for key in ("session_date",):
                value = row.get(key)
                if isinstance(value, dt.date):
                    row[key] = value.isoformat()
            for key in ("ref_ts", "computed_at"):
                value = row.get(key)
                if isinstance(value, dt.datetime):
                    row[key] = value.isoformat()
        return rows

    def summary(self, symbol: str, *, window_days: int = 90) -> dict[str, object] | None:
        """Souhrn okna: podíl close uvnitř pásma + četnost dotyků; None bez dat."""
        since = dt.date.today() - dt.timedelta(days=window_days)
        stmt = select(
            func.count(),
            func.sum(func.cast(em_respect_table.c.close_in_band, Integer)),
            func.sum(func.cast(em_respect_table.c.touch_upper, Integer)),
            func.sum(func.cast(em_respect_table.c.touch_lower, Integer)),
        ).where(
            em_respect_table.c.symbol == symbol,
            em_respect_table.c.session_date >= since,
        )
        with self._engine.connect() as conn:
            total, in_band, touch_up, touch_down = conn.execute(stmt).one()
        if not total:
            return None
        return {
            "window_days": window_days,
            "n": int(total),
            "close_in_band_share": int(in_band or 0) / int(total),
            "touch_upper_share": int(touch_up or 0) / int(total),
            "touch_lower_share": int(touch_down or 0) / int(total),
        }
