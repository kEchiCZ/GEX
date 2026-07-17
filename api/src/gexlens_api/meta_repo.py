"""CRUD repository nad metadata tabulkami (issue #21, SPEC 5.3 + kap. 6).

Engine (SQLAlchemy) se vytváří líně při prvním použití — API server bez
CRUD provozu se k databázi vůbec nepřipojí.
"""

import datetime as dt
import threading
from typing import Any

from sqlalchemy import create_engine, delete, insert, select, update
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.exc import IntegrityError

from gexlens_engine.config import Settings
from gexlens_engine.storage.meta import (
    alerts_table,
    annotations_table,
    meta_metadata,
    settings_table,
    watchlist_table,
)


class DuplicateEntryError(ValueError):
    """Porušení unikátnosti (např. symbol už ve watchlistu) → HTTP 409."""


class NotFoundError(LookupError):
    """Záznam neexistuje → HTTP 404."""


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
                meta_metadata.create_all(self._engine)
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
        except IntegrityError as exc:
            raise DuplicateEntryError(f"Symbol {symbol!r} už ve watchlistu je") from exc
        return {"id": item_id, "symbol": symbol}

    def watchlist_remove(self, item_id: int) -> None:
        with self._db().begin() as conn:
            result = conn.execute(delete(watchlist_table).where(watchlist_table.c.id == item_id))
            if result.rowcount == 0:
                raise NotFoundError(f"Watchlist položka {item_id} neexistuje")

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
