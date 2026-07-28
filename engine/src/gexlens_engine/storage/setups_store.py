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
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine

from gexlens_engine.compute.setups import SETUP_MECHANICS_VERSION
from gexlens_engine.compute.setupstats import ClosedSetup

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
    # Verze mechaniky, která setup vyrobila (#311) — statistiky a kalibrace
    # počítají jen aktuální, aby se nemíchaly výsledky různých systémů.
    # Řádky z doby před zavedením sloupce dostanou 1 (viz `ensure_schema`).
    Column("mechanics_version", Integer, nullable=False, server_default="1"),
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
        self._ensure_mechanics_version()

    def _ensure_mechanics_version(self) -> None:
        """Doplní sloupec `mechanics_version` do existující tabulky (#311).

        `create_all` existující tabulku nemění, takže nasazené instance by
        sloupec nedostaly. ALTER je idempotentní přes kontrolu inspektorem
        a běží na PG i sqlite (testy). Staré řádky tím dostanou verzi 1 —
        hranice tak vyjde přirozeně správně: všechno před touto migrací
        vzniklo starou mechanikou nebo nad zmrzlými daty (ADR-0015).
        """
        inspector = inspect(self._engine)
        if not inspector.has_table(setups_table.name):
            return
        columns = {col["name"] for col in inspector.get_columns(setups_table.name)}
        if "mechanics_version" in columns:
            return
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    f"ALTER TABLE {setups_table.name} "
                    "ADD COLUMN mechanics_version INTEGER NOT NULL DEFAULT 1"
                )
            )

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
            mechanics_version=SETUP_MECHANICS_VERSION,
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

    def closed_since(
        self, symbol: str, since: dt.datetime, *, mechanics_version: int | None = None
    ) -> list[ClosedSetup]:
        """Uzavřené setupy s `closed_ts` od `since` — podklad sebekontroly (#309).

        Řadí se podle času uzavření, ne vzniku: setup otevřený před oknem, ale
        uzavřený v něm, do bilance okna patří.

        `mechanics_version` omezí bilanci na jeden systém (#311) — bez něj by se
        míchaly výsledky staré a nové mechaniky a verdikt by mluvil o minulosti.
        """
        stmt = select(
            setups_table.c.template,
            setups_table.c.direction,
            setups_table.c.status,
            setups_table.c.outcome_r,
        ).where(
            setups_table.c.symbol == symbol,
            setups_table.c.status != "active",
            setups_table.c.closed_ts.is_not(None),
            setups_table.c.closed_ts >= since,
        )
        if mechanics_version is not None:
            stmt = stmt.where(setups_table.c.mechanics_version == mechanics_version)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [
            ClosedSetup(
                template=row.template,
                direction=row.direction,
                status=row.status,
                outcome_r=float(row.outcome_r or 0.0),
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
