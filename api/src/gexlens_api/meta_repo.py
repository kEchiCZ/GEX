"""CRUD repository nad metadata tabulkami (issue #21, SPEC 5.3 + kap. 6).

Engine (SQLAlchemy) se vytváří líně při prvním použití — API server bez
CRUD provozu se k databázi vůbec nepřipojí.
"""

import datetime as dt
import threading
from typing import Any

from sqlalchemy import create_engine, delete, insert, select, text, update
from sqlalchemy.engine import Connection, CursorResult, Engine
from sqlalchemy.exc import IntegrityError

from gexlens_engine.config import Settings
from gexlens_engine.storage.meta import (
    WATCHLIST_CHANNEL,
    alerts_table,
    annotations_table,
    ensure_meta_schema,
    journal_table,
    journal_trades_table,
    settings_table,
    watchlist_table,
)


def _notify_watchlist(conn: Connection, payload: str) -> None:
    """PG NOTIFY změny watchlistu (#207) — engine startuje sběr do sekund.

    Volá se uvnitř transakce (NOTIFY se doručí až s commitem, takže engine
    nikdy nečte watchlist před zápisem). Mimo PostgreSQL (SQLite testy) no-op.
    """
    if conn.dialect.name != "postgresql":
        return
    conn.execute(
        text("select pg_notify(:channel, :payload)"),
        {
            "channel": WATCHLIST_CHANNEL,
            "payload": payload,
        },
    )


class DuplicateEntryError(ValueError):
    """Porušení unikátnosti (např. symbol už ve watchlistu) → HTTP 409."""


class NotFoundError(LookupError):
    """Záznam neexistuje → HTTP 404."""


def _journal_row(entry: dict[str, Any], trade: dict[str, Any] | None) -> dict[str, Any]:
    """Řádek deníku pro API: profil s doplněným defaultem + vnořený obchod.

    Záznamy z fáze A (#673) nemají `profile` — čtou se jako `smb`. Doplňuje
    se až tady, aby v databázi zůstalo poznat, že hodnota nebyla zadaná.
    """
    row = dict(entry)
    row["profile"] = row.get("profile") or "smb"
    if trade is not None:
        nested = {key: value for key, value in trade.items() if key not in ("id", "entry_id")}
        row["trade"] = nested
    else:
        row["trade"] = None
    return row


def _inserted_id(result: CursorResult[Any]) -> int:
    primary_key = result.inserted_primary_key
    if primary_key is None:
        raise RuntimeError("INSERT nevrátil primární klíč")
    return int(primary_key[0])


class MetaRepository:
    def __init__(self, settings: Settings) -> None:
        self._url = settings.database_url
        self._engine: Engine | None = None
        self._lock = threading.Lock()

    def _db(self) -> Engine:
        with self._lock:
            if self._engine is None:
                self._engine = create_engine(self._url)
                ensure_meta_schema(self._engine)
            return self._engine

    def engine(self) -> Engine:
        """Sdílený DB engine (lazy) — např. pro čtení OI archivu v /replay."""
        return self._db()

    # ── watchlist ──────────────────────────────────────────────────

    def watchlist(self) -> list[dict[str, Any]]:
        with self._db().connect() as conn:
            rows = conn.execute(select(watchlist_table).order_by(watchlist_table.c.id))
            return [dict(row._mapping) for row in rows]

    def watchlist_add(self, symbol: str) -> dict[str, Any]:
        try:
            with self._db().begin() as conn:
                result = conn.execute(insert(watchlist_table).values(symbol=symbol))
                item_id = _inserted_id(result)
                _notify_watchlist(conn, symbol)
        except IntegrityError as exc:
            raise DuplicateEntryError(f"Symbol {symbol!r} už ve watchlistu je") from exc
        return {"id": item_id, "symbol": symbol}

    def watchlist_remove(self, item_id: int) -> None:
        with self._db().begin() as conn:
            result = conn.execute(delete(watchlist_table).where(watchlist_table.c.id == item_id))
            if result.rowcount == 0:
                raise NotFoundError(f"Watchlist položka {item_id} neexistuje")
            _notify_watchlist(conn, "")

    # ── alerts ─────────────────────────────────────────────────────

    def alerts(self) -> list[dict[str, Any]]:
        with self._db().connect() as conn:
            rows = conn.execute(select(alerts_table).order_by(alerts_table.c.id))
            return [dict(row._mapping) for row in rows]

    def alert_create(
        self, symbol: str, kind: str, params: dict[str, Any], enabled: bool
    ) -> dict[str, Any]:
        with self._db().begin() as conn:
            result = conn.execute(
                insert(alerts_table).values(
                    symbol=symbol, kind=kind, params=params, enabled=enabled
                )
            )
            alert_id = _inserted_id(result)
        return {
            "id": alert_id,
            "symbol": symbol,
            "kind": kind,
            "params": params,
            "enabled": enabled,
        }

    def alert_update(self, alert_id: int, **fields: Any) -> dict[str, Any]:
        with self._db().begin() as conn:
            result = conn.execute(
                update(alerts_table).where(alerts_table.c.id == alert_id).values(**fields)
            )
            if result.rowcount == 0:
                raise NotFoundError(f"Alert {alert_id} neexistuje")
            row = conn.execute(select(alerts_table).where(alerts_table.c.id == alert_id)).one()
            return dict(row._mapping)

    def alert_delete(self, alert_id: int) -> None:
        with self._db().begin() as conn:
            result = conn.execute(delete(alerts_table).where(alerts_table.c.id == alert_id))
            if result.rowcount == 0:
                raise NotFoundError(f"Alert {alert_id} neexistuje")

    # ── journal (#673 fáze A, #709 rev. 2) ─────────────────────────

    def journal_list(
        self,
        *,
        symbol: str | None = None,
        day: dt.date | None = None,
        entry_type: str | None = None,
        profile: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        stmt = select(journal_table).order_by(journal_table.c.ts_ref.desc()).limit(limit)
        if symbol is not None:
            stmt = stmt.where(journal_table.c.symbol == symbol)
        if day is not None:
            start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC)
            stmt = stmt.where(
                journal_table.c.ts_ref >= start,
                journal_table.c.ts_ref < start + dt.timedelta(days=1),
            )
        if entry_type is not None:
            stmt = stmt.where(journal_table.c.entry_type == entry_type)
        if profile is not None:
            # Řádky z fáze A mají NULL a čtou se jako `smb` — filtr to musí
            # respektovat, jinak by starší záznamy z výběru „smb" vypadly.
            if profile == "smb":
                stmt = stmt.where(
                    (journal_table.c.profile == "smb") | journal_table.c.profile.is_(None)
                )
            else:
                stmt = stmt.where(journal_table.c.profile == profile)
        with self._db().connect() as conn:
            entries = [dict(row._mapping) for row in conn.execute(stmt)]
            trades = self._journal_trades(conn, [int(entry["id"]) for entry in entries])
        return [_journal_row(entry, trades.get(int(entry["id"]))) for entry in entries]

    def journal_symbols(self) -> list[str]:
        """Symboly, které mají v deníku aspoň jeden záznam — pro filtr v UI."""
        stmt = select(journal_table.c.symbol).distinct().order_by(journal_table.c.symbol)
        with self._db().connect() as conn:
            return [str(row[0]) for row in conn.execute(stmt)]

    def journal_create(
        self, values: dict[str, Any], trade: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        with self._db().begin() as conn:
            result = conn.execute(insert(journal_table).values(**values))
            entry_id = _inserted_id(result)
            if trade is not None:
                conn.execute(insert(journal_trades_table).values(entry_id=entry_id, **trade))
            return self._journal_read(conn, entry_id)

    def journal_update(
        self,
        entry_id: int,
        values: dict[str, Any],
        trade: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._db().begin() as conn:
            result = conn.execute(
                update(journal_table).where(journal_table.c.id == entry_id).values(**values)
            )
            if result.rowcount == 0:
                raise NotFoundError(f"Záznam deníku {entry_id} neexistuje")
            if trade is not None:
                updated = conn.execute(
                    update(journal_trades_table)
                    .where(journal_trades_table.c.entry_id == entry_id)
                    .values(**trade)
                )
                if updated.rowcount == 0:
                    # Záznam mohl vzniknout bez obchodu (např. povýšení
                    # pozorování na obchod) — doplníme řádek, ne chybu.
                    conn.execute(insert(journal_trades_table).values(entry_id=entry_id, **trade))
            return self._journal_read(conn, entry_id)

    def journal_delete(self, entry_id: int) -> None:
        with self._db().begin() as conn:
            # SQLite nevynucuje ON DELETE CASCADE bez PRAGMA foreign_keys,
            # takže navázaný obchod mažeme výslovně.
            conn.execute(
                delete(journal_trades_table).where(journal_trades_table.c.entry_id == entry_id)
            )
            result = conn.execute(delete(journal_table).where(journal_table.c.id == entry_id))
            if result.rowcount == 0:
                raise NotFoundError(f"Záznam deníku {entry_id} neexistuje")

    def _journal_read(self, conn: Connection, entry_id: int) -> dict[str, Any]:
        row = conn.execute(select(journal_table).where(journal_table.c.id == entry_id)).one()
        trades = self._journal_trades(conn, [entry_id])
        return _journal_row(dict(row._mapping), trades.get(entry_id))

    @staticmethod
    def _journal_trades(conn: Connection, entry_ids: list[int]) -> dict[int, dict[str, Any]]:
        if not entry_ids:
            return {}
        rows = conn.execute(
            select(journal_trades_table).where(journal_trades_table.c.entry_id.in_(entry_ids))
        )
        return {int(row._mapping["entry_id"]): dict(row._mapping) for row in rows}

    # ── annotations ────────────────────────────────────────────────

    def annotations(self, symbol: str, day: dt.date) -> list[dict[str, Any]]:
        stmt = (
            select(annotations_table)
            .where(annotations_table.c.symbol == symbol, annotations_table.c.day == day)
            .order_by(annotations_table.c.id)
        )
        with self._db().connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def annotation_create(
        self, symbol: str, day: dt.date, payload: dict[str, Any]
    ) -> dict[str, Any]:
        with self._db().begin() as conn:
            result = conn.execute(
                insert(annotations_table).values(symbol=symbol, day=day, payload=payload)
            )
            annotation_id = _inserted_id(result)
        return {"id": annotation_id, "symbol": symbol, "day": day, "payload": payload}

    def annotation_update(self, annotation_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Přepíše payload anotace — přesun tažením (#589) drží `id`, ať undo ví na co mířit."""
        with self._db().begin() as conn:
            result = conn.execute(
                update(annotations_table)
                .where(annotations_table.c.id == annotation_id)
                .values(payload=payload)
            )
            if result.rowcount == 0:
                raise NotFoundError(f"Anotace {annotation_id} neexistuje")
            row = conn.execute(
                select(annotations_table).where(annotations_table.c.id == annotation_id)
            ).one()
            return dict(row._mapping)

    def annotation_delete(self, annotation_id: int) -> None:
        with self._db().begin() as conn:
            result = conn.execute(
                delete(annotations_table).where(annotations_table.c.id == annotation_id)
            )
            if result.rowcount == 0:
                raise NotFoundError(f"Anotace {annotation_id} neexistuje")

    # ── settings ───────────────────────────────────────────────────

    def settings_all(self) -> dict[str, Any]:
        with self._db().connect() as conn:
            rows = conn.execute(select(settings_table))
            return {row.key: row.value for row in rows}

    def setting_put(self, key: str, value: Any) -> None:
        with self._db().begin() as conn:
            result = conn.execute(
                update(settings_table).where(settings_table.c.key == key).values(value=value)
            )
            if result.rowcount == 0:
                conn.execute(insert(settings_table).values(key=key, value=value))
