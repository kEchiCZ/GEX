"""Tabulka `iv_rank` (#871) — denní IV a její kontext per zdroj.

Tři řady vedle sebe, NIKDY se nemíchají do jednoho percentilu (jiné
konstrukce; sonda 26. 8.: IBKR 30d index ES 0,119 vs. tasty IV index 0,156):

* `ibkr` — 30d IV index podkladu z `OPTION_IMPLIED_VOLATILITY` (backfill
  rok zpět, pak denní dotažení). Primární řada: plná historie od prvního dne.
* `tasty` — hotový rank/percentile z /market-metrics (křížová kontrola;
  historie jen ode dneška, jejich čísla se přebírají, nepočítají).
* `own_atm` — vlastní ATM IV z věčného `oi_eod` (#519), tenor ~7 dní.
  Dlouhodobě nezávislá kotva; percentil až od MIN_SAMPLE vzorků.

PostgreSQL navždy; rank i percentil se ukládají oba (rank = poloha mezi
min/max okna, percentil = podíl dnů pod hodnotou) + verze definice.
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

iv_rank_metadata = MetaData()

IV_RANK_VERSION = 1

SOURCE_IBKR = "ibkr"
SOURCE_TASTY = "tasty"
SOURCE_OWN_ATM = "own_atm"

iv_rank_table = Table(
    "iv_rank",
    iv_rank_metadata,
    Column("session_date", Date, primary_key=True),
    Column("symbol", String(16), primary_key=True),
    Column("source", String(12), primary_key=True),
    Column("iv", Float, nullable=False),
    # NULL = málo vzorků (MIN_SAMPLE, vzor ADR-0028) — žádný default
    Column("iv_rank", Float, nullable=True),
    Column("iv_percentile", Float, nullable=True),
    Column("sample", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False),
)


class IvRankRepository:
    """Upsert per (session_date, symbol, source) — idempotentní pro backfill."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        iv_rank_metadata.create_all(self._engine)

    def series(self, symbol: str, source: str) -> list[tuple[dt.date, float]]:
        """(den, iv) vzestupně — vstup pro rank/percentil klouzavého okna."""
        stmt = (
            select(iv_rank_table.c.session_date, iv_rank_table.c.iv)
            .where(iv_rank_table.c.symbol == symbol, iv_rank_table.c.source == source)
            .order_by(iv_rank_table.c.session_date)
        )
        with self._engine.connect() as conn:
            return [(row.session_date, float(row.iv)) for row in conn.execute(stmt)]

    def upsert(
        self,
        *,
        session_date: dt.date,
        symbol: str,
        source: str,
        iv: float,
        iv_rank: float | None,
        iv_percentile: float | None,
        sample: int,
        computed_at: dt.datetime,
    ) -> None:
        values = {
            "session_date": session_date,
            "symbol": symbol,
            "source": source,
            "iv": iv,
            "iv_rank": iv_rank,
            "iv_percentile": iv_percentile,
            "sample": sample,
            "version": IV_RANK_VERSION,
            "computed_at": computed_at,
        }
        with self._engine.begin() as conn:
            updated = conn.execute(
                update(iv_rank_table)
                .where(
                    iv_rank_table.c.session_date == session_date,
                    iv_rank_table.c.symbol == symbol,
                    iv_rank_table.c.source == source,
                )
                .values(**values)
            )
            if updated.rowcount == 0:
                conn.execute(iv_rank_table.insert().values(**values))

    def latest(self, symbol: str) -> list[dict[str, object]]:
        """Poslední řádek KAŽDÉHO zdroje — hlavička/briefing čtou všechny tři."""
        out: list[dict[str, object]] = []
        for source in (SOURCE_IBKR, SOURCE_TASTY, SOURCE_OWN_ATM):
            stmt = (
                select(iv_rank_table)
                .where(iv_rank_table.c.symbol == symbol, iv_rank_table.c.source == source)
                .order_by(iv_rank_table.c.session_date.desc())
                .limit(1)
            )
            with self._engine.connect() as conn:
                row = conn.execute(stmt).first()
            if row is None:
                continue
            record = dict(row._mapping)
            record["session_date"] = row.session_date.isoformat()
            record["computed_at"] = row.computed_at.isoformat()
            out.append(record)
        return out

    def list_for(self, symbol: str, *, limit: int = 400) -> list[dict[str, object]]:
        stmt = (
            select(iv_rank_table)
            .where(iv_rank_table.c.symbol == symbol)
            .order_by(iv_rank_table.c.session_date.desc())
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
