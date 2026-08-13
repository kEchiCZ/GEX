"""Porovnávací tabulka feedů (#613, shadow fáze M7).

Jediný výstup shadow módu: řádek per (minuta, kontrakt, pole) s hodnotou
z obou zdrojů. Nic z ní nečtou výpočty ani UI — jen vyhodnocovací skript
(`scripts/feed_comparison_report.py`), jehož čísla kalibrují prahy hystereze
fáze 2. Tabulka je dočasná pracovní: po skončení shadow sběru se flag vypne
a data zůstávají jako doklad.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    insert,
)
from sqlalchemy.engine import Engine

feed_metadata = MetaData()

feed_comparison_table = Table(
    "feed_comparison",
    feed_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("symbol", String(48), nullable=False),  # popisek kontraktu (expiry strike right)
    Column("field", String(16), nullable=False),  # bid/ask/iv/delta/gamma/oi/spot
    # NULL = zdroj hodnotu v okně max stáří neměl — „chybějící" pro report
    Column("value_ibkr", Float, nullable=True),
    Column("value_tasty", Float, nullable=True),
    Column("delta", Float, nullable=True),
    Column("age_ibkr_ms", BigInteger, nullable=True),
    Column("age_tasty_ms", BigInteger, nullable=True),
)


@dataclass(frozen=True)
class ComparisonRow:
    ts: dt.datetime
    symbol: str
    field: str
    value_ibkr: float | None
    value_tasty: float | None
    age_ibkr_ms: int | None
    age_tasty_ms: int | None

    @property
    def delta(self) -> float | None:
        if self.value_ibkr is None or self.value_tasty is None:
            return None
        return self.value_tasty - self.value_ibkr


class FeedComparisonRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        feed_metadata.create_all(self._engine)

    def insert_many(self, rows: Sequence[ComparisonRow]) -> None:
        if not rows:
            return
        payload = [
            {
                "ts": row.ts,
                "symbol": row.symbol,
                "field": row.field,
                "value_ibkr": row.value_ibkr,
                "value_tasty": row.value_tasty,
                "delta": row.delta,
                "age_ibkr_ms": row.age_ibkr_ms,
                "age_tasty_ms": row.age_tasty_ms,
            }
            for row in rows
        ]
        with self._engine.begin() as conn:
            conn.execute(insert(feed_comparison_table), payload)
