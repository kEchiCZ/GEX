"""Úložiště paper účtu (#1187 fáze 1, ADR-0040): účet, ordery, události, zápis do deníku.

Jeden účet (id 1) v jednotkách plného kontraktu; ordery denní; uzavřený
obchod se zapíše do deníku (`journal_entries` + `journal_trades`, typ
`obchod`, tag `paper`) — deník je vstup kouče (#933), proto se plní
z filů simulátoru, ne z toho, co uživatel klikl.
"""

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
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

from gexlens_engine.compute.paper import PaperOrder
from gexlens_engine.compute.risk import RealizedSetup
from gexlens_engine.storage.meta import journal_table, journal_trades_table

paper_metadata = MetaData()

paper_accounts_table = Table(
    "paper_accounts",
    paper_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(64), nullable=False),
    Column("equity_start", Float, nullable=False),
    Column("created_ts", DateTime(timezone=True), nullable=False),
    Column("halted", Boolean, nullable=False, default=False),
    Column("halted_reason", Text, nullable=True),
    Column("halted_ts", DateTime(timezone=True), nullable=True),
)

paper_events_table = Table(
    "paper_events",
    paper_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account_id", Integer, nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("kind", String(16), nullable=False),  # deposit | withdraw | note
    Column("amount", Float, nullable=False, default=0.0),
    Column("note", Text, nullable=True),
)

paper_orders_table = Table(
    "paper_orders",
    paper_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account_id", Integer, nullable=False),
    Column("symbol", String(16), nullable=False),
    Column("side", String(8), nullable=False),
    Column("qty", Integer, nullable=False),
    Column("order_type", String(8), nullable=False),
    Column("entry_price", Float, nullable=False),
    Column("stop_price", Float, nullable=False),
    Column("target_price", Float, nullable=True),
    Column("status", String(16), nullable=False),
    Column("created_ts", DateTime(timezone=True), nullable=False),
    Column("filled_ts", DateTime(timezone=True), nullable=True),
    Column("fill_price", Float, nullable=True),
    Column("closed_ts", DateTime(timezone=True), nullable=True),
    Column("exit_price", Float, nullable=True),
    Column("exit_reason", String(16), nullable=True),
    Column("pnl_usd", Float, nullable=True),
    Column("fees_usd", Float, nullable=True),
    Column("r_multiple", Float, nullable=True),
    Column("risk_usd", Float, nullable=False),
    Column("point_value", Float, nullable=False),
    Column("setup_key", String(64), nullable=True),
    Column("setup_id", Integer, nullable=True),
    Column("note", Text, nullable=True),
    Column("context", JSON, nullable=False, default=dict),
    Column("close_requested", Boolean, nullable=False, default=False),
    Column("mfe", Float, nullable=True),
    Column("mae", Float, nullable=True),
    Column("journal_entry_id", Integer, nullable=True),
)

DEFAULT_ACCOUNT_ID = 1
DEFAULT_EQUITY_START = 50000.0
ACTIVE_STATUSES = ("working", "open")


@dataclass(frozen=True)
class Account:
    id: int
    name: str
    equity_start: float
    halted: bool
    halted_reason: str | None


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, dt.datetime) else value


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value


class PaperRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        paper_metadata.create_all(self._engine)
        inspector = inspect(self._engine)
        if not inspector.has_table(paper_accounts_table.name):
            return
        with self._engine.begin() as conn:
            existing = conn.execute(
                select(paper_accounts_table.c.id).where(
                    paper_accounts_table.c.id == DEFAULT_ACCOUNT_ID
                )
            ).first()
            if existing is None:
                conn.execute(
                    insert(paper_accounts_table).values(
                        id=DEFAULT_ACCOUNT_ID,
                        name="paper",
                        equity_start=DEFAULT_EQUITY_START,
                        created_ts=dt.datetime.now(dt.UTC),
                        halted=False,
                    )
                )

    # ── účet ─────────────────────────────────────────────────────

    def account(self, account_id: int = DEFAULT_ACCOUNT_ID) -> Account | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(paper_accounts_table).where(paper_accounts_table.c.id == account_id)
            ).first()
        if row is None:
            return None
        return Account(
            id=row.id,
            name=row.name,
            equity_start=float(row.equity_start),
            halted=bool(row.halted),
            halted_reason=row.halted_reason,
        )

    def set_halted(
        self,
        halted: bool,
        reason: str | None,
        now: dt.datetime,
        account_id: int = DEFAULT_ACCOUNT_ID,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(paper_accounts_table)
                .where(paper_accounts_table.c.id == account_id)
                .values(halted=halted, halted_reason=reason, halted_ts=now if halted else None)
            )

    def add_event(
        self,
        kind: str,
        amount: float,
        note: str | None,
        now: dt.datetime,
        account_id: int = DEFAULT_ACCOUNT_ID,
    ) -> int:
        with self._engine.begin() as conn:
            result = conn.execute(
                insert(paper_events_table).values(
                    account_id=account_id, ts=now, kind=kind, amount=amount, note=note
                )
            )
        key = result.inserted_primary_key
        assert key is not None
        return int(key[0])

    def events(
        self, account_id: int = DEFAULT_ACCOUNT_ID, limit: int = 100
    ) -> list[dict[str, Any]]:
        stmt = (
            select(paper_events_table)
            .where(paper_events_table.c.account_id == account_id)
            .order_by(paper_events_table.c.ts.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            return [{k: _iso(v) for k, v in dict(r._mapping).items()} for r in conn.execute(stmt)]

    def equity(self, account_id: int = DEFAULT_ACCOUNT_ID) -> float:
        """Start + vklady − výběry + Σ P/L uzavřených obchodů (po poplatcích)."""
        account = self.account(account_id)
        if account is None:
            return 0.0
        with self._engine.connect() as conn:
            events = conn.execute(
                select(paper_events_table.c.kind, paper_events_table.c.amount).where(
                    paper_events_table.c.account_id == account_id
                )
            ).fetchall()
            pnl = conn.execute(
                select(paper_orders_table.c.pnl_usd).where(
                    paper_orders_table.c.account_id == account_id,
                    paper_orders_table.c.status == "closed",
                )
            ).fetchall()
        flows = sum(
            float(row.amount) if row.kind == "deposit" else -float(row.amount)
            for row in events
            if row.kind in ("deposit", "withdraw")
        )
        return account.equity_start + flows + sum(float(row.pnl_usd or 0.0) for row in pnl)

    # ── ordery ───────────────────────────────────────────────────

    def create_order(self, values: dict[str, Any]) -> int:
        payload = dict(values)
        payload["context"] = json.loads(json.dumps(payload.get("context") or {}, default=str))
        with self._engine.begin() as conn:
            result = conn.execute(insert(paper_orders_table).values(**payload))
        key = result.inserted_primary_key
        assert key is not None
        return int(key[0])

    def update_order(self, order_id: int, **values: Any) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(paper_orders_table)
                .where(paper_orders_table.c.id == order_id)
                .values(**values)
            )

    def get_order(self, order_id: int) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(paper_orders_table).where(paper_orders_table.c.id == order_id)
            ).first()
        return None if row is None else {k: _iso(v) for k, v in dict(row._mapping).items()}

    def list_orders(
        self,
        *,
        symbol: str | None = None,
        status: str | None = None,
        limit: int = 200,
        account_id: int = DEFAULT_ACCOUNT_ID,
    ) -> list[dict[str, Any]]:
        stmt = select(paper_orders_table).where(paper_orders_table.c.account_id == account_id)
        if symbol is not None:
            stmt = stmt.where(paper_orders_table.c.symbol == symbol)
        if status is not None:
            stmt = stmt.where(paper_orders_table.c.status == status)
        stmt = stmt.order_by(paper_orders_table.c.created_ts.desc()).limit(limit)
        with self._engine.connect() as conn:
            return [{k: _iso(v) for k, v in dict(r._mapping).items()} for r in conn.execute(stmt)]

    def active(
        self, symbol: str | None = None, account_id: int = DEFAULT_ACCOUNT_ID
    ) -> list[PaperOrder]:
        """Čekající a otevřené ordery jako model pro simulátor."""
        stmt = select(paper_orders_table).where(
            paper_orders_table.c.account_id == account_id,
            paper_orders_table.c.status.in_(ACTIVE_STATUSES),
        )
        if symbol is not None:
            stmt = stmt.where(paper_orders_table.c.symbol == symbol)
        stmt = stmt.order_by(paper_orders_table.c.id)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [
            PaperOrder(
                id=int(row.id),
                symbol=str(row.symbol),
                side=row.side,
                qty=int(row.qty),
                order_type=row.order_type,
                entry_price=float(row.entry_price),
                stop_price=float(row.stop_price),
                target_price=float(row.target_price) if row.target_price is not None else None,
                status=row.status,
                fill_price=float(row.fill_price) if row.fill_price is not None else None,
                filled_ts=_aware(row.filled_ts),
                close_requested=bool(row.close_requested),
                mfe=float(row.mfe or 0.0),
                mae=float(row.mae or 0.0),
            )
            for row in rows
        ]

    def realized_since(
        self, since: dt.datetime, account_id: int = DEFAULT_ACCOUNT_ID
    ) -> list[RealizedSetup]:
        """Uzavřené obchody od `since` jako vstup brzd (`compute.risk.brake_state`)."""
        stmt = select(
            paper_orders_table.c.symbol,
            paper_orders_table.c.status,
            paper_orders_table.c.r_multiple,
            paper_orders_table.c.closed_ts,
            paper_orders_table.c.exit_reason,
        ).where(
            paper_orders_table.c.account_id == account_id,
            paper_orders_table.c.status == "closed",
            paper_orders_table.c.closed_ts >= since,
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        result: list[RealizedSetup] = []
        for row in rows:
            closed = _aware(row.closed_ts)
            assert closed is not None
            result.append(
                RealizedSetup(
                    symbol=str(row.symbol),
                    template="paper",
                    status="closed_stop" if row.exit_reason == "stop" else "closed_target",
                    outcome_r=float(row.r_multiple or 0.0),
                    closed_ts=closed,
                    tradeable=True,
                    affordable=True,
                )
            )
        return result

    # ── deník ────────────────────────────────────────────────────

    def journal_trade(
        self, order: dict[str, Any], *, text_body: str, context: dict[str, Any]
    ) -> int | None:
        """Zápis uzavřeného obchodu do deníku (typ `obchod`, tag `paper`).

        Tabulky deníku patří meta schématu (zakládá je API) — když neexistují,
        zápis se přeskočí a vrátí None, simulátor tím nesmí spadnout.
        """
        inspector = inspect(self._engine)
        if not inspector.has_table(journal_table.name) or not inspector.has_table(
            journal_trades_table.name
        ):
            return None
        filled = order.get("filled_ts")
        closed = order.get("closed_ts")
        ts_ref = dt.datetime.fromisoformat(filled) if isinstance(filled, str) else filled
        closed_ts = dt.datetime.fromisoformat(closed) if isinstance(closed, str) else closed
        entry_values = {
            "ts_ref": ts_ref or closed_ts or dt.datetime.now(dt.UTC),
            "symbol": order["symbol"],
            "entry_type": "obchod",
            "text": text_body,
            "tags": ["paper"],
            "setup_id": order.get("setup_id"),
            "profile": "futures",
            "context": json.loads(json.dumps(context, default=str)),
            "created_ts": dt.datetime.now(dt.UTC),
        }
        trade_values = {
            "direction": order["side"],
            "planned_entry": order["entry_price"],
            "planned_stop": order["stop_price"],
            "planned_target": order.get("target_price"),
            "actual_entry": order.get("fill_price"),
            "actual_exit": order.get("exit_price"),
            "size": float(order["qty"]),
            "opened_ts": ts_ref,
            "closed_ts": closed_ts,
            "setup_key": order.get("setup_key"),
            "mistake_tags": [],
            "mfe": order.get("mfe"),
            "mae": order.get("mae"),
            "gross_pnl": (order.get("pnl_usd") or 0.0) + (order.get("fees_usd") or 0.0),
            "net_pnl": order.get("pnl_usd"),
            "fees": order.get("fees_usd"),
        }
        with self._engine.begin() as conn:
            result = conn.execute(insert(journal_table).values(**entry_values))
            key = result.inserted_primary_key
            assert key is not None
            entry_id = int(key[0])
            conn.execute(insert(journal_trades_table).values(entry_id=entry_id, **trade_values))
            conn.execute(
                update(paper_orders_table)
                .where(paper_orders_table.c.id == order["id"])
                .values(journal_entry_id=entry_id)
            )
        return entry_id

    def ensure_journal_column(self) -> None:  # pragma: no cover — jen PG migrace
        """Idempotentní ADD COLUMN pro budoucí sloupce (vzor #311)."""
        inspector = inspect(self._engine)
        if not inspector.has_table(paper_orders_table.name):
            return
        columns = {col["name"] for col in inspector.get_columns(paper_orders_table.name)}
        if "journal_entry_id" not in columns:
            with self._engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE paper_orders ADD COLUMN journal_entry_id INTEGER NULL")
                )
