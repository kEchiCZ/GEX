"""Verzovaný parameter store setupů (#794 fáze 2, ADR-0033).

Prahy šablon (`compute.setups.SetupParams`) byly konstanty v kódu a osm z nich
šlo přepsat z `.env`. Samoučící smyčka potřebuje parametry **verzované a
auditované**: kdo, kdy, proč a s jakou hodnotou — a každý setup musí nést
verzi, se kterou vznikl, aby šel track record rozdělit podle parametrů,
aniž by se lámala verze mechaniky (`mechanics_version` = sémantika
stopů/cílů; `params_version` = jen prahy).

Tabulka `setup_params` je append-only: nová verze = nový řádek, poslední
řádek platí. Nic se nemaže ani nepřepisuje (R4 duch, stejně jako `setups`).
Engine při startu bez řádku založí **seed** z parametrů, se kterými nastartoval
(defaulty + `.env`) — první nasazení tak nezmění chování ani o vlas.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Integer, String, Table, Text, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.compute.setups import (
    SETUP_MECHANICS_VERSION,
    SetupParams,
    params_from_dict,
    params_to_dict,
)
from gexlens_engine.storage.setups_store import setups_metadata

setup_params_table = Table(
    "setup_params",
    setups_metadata,
    # id = verze parametrů; roste monotónně, poslední řádek platí
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_ts", DateTime(timezone=True), nullable=False),
    # Kdo verzi založil: "engine" (seed při startu), "ui", "script", …
    Column("created_by", String(32), nullable=False),
    # Proč — povinné (audit); bez důvodu se verze nezakládá
    Column("note", Text, nullable=False),
    # Mechanika, za které verze vznikla — párování s `setups.mechanics_version`
    Column("mechanics_version", Integer, nullable=False),
    Column("params", JSON, nullable=False),
)


@dataclass(frozen=True)
class StoredParams:
    version: int
    created_ts: dt.datetime
    created_by: str
    note: str
    mechanics_version: int
    params: SetupParams

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly podoba pro API (parametry jako plochý dict)."""
        return {
            "version": self.version,
            "created_ts": self.created_ts.isoformat(),
            "created_by": self.created_by,
            "note": self.note,
            "mechanics_version": self.mechanics_version,
            "params": params_to_dict(self.params),
        }


class SetupParamsRepository:
    """Append-only verze parametrů setupů; čte engine i API."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        setup_params_table.create(self._engine, checkfirst=True)

    def save(self, params: SetupParams, *, note: str, created_by: str) -> StoredParams:
        """Založí novou verzi. Prázdná poznámka je chyba — audit bez důvodu nemá cenu."""
        if not note.strip():
            raise ValueError("Verze parametrů potřebuje poznámku (proč se mění)")
        now = dt.datetime.now(dt.UTC)
        stmt = insert(setup_params_table).values(
            created_ts=now,
            created_by=created_by,
            note=note.strip(),
            mechanics_version=SETUP_MECHANICS_VERSION,
            params=json.loads(json.dumps(params_to_dict(params))),
        )
        with self._engine.begin() as conn:
            result = conn.execute(stmt)
        key = result.inserted_primary_key
        if key is None:
            raise RuntimeError("Insert verze parametrů nevrátil primární klíč")
        return StoredParams(
            version=int(key[0]),
            created_ts=now,
            created_by=created_by,
            note=note.strip(),
            mechanics_version=SETUP_MECHANICS_VERSION,
            params=params,
        )

    def latest(self) -> StoredParams | None:
        """Platná verze (poslední řádek); None = tabulka prázdná (před seedem)."""
        rows = self.history(limit=1)
        return rows[0] if rows else None

    def latest_version(self) -> int | None:
        """Jen číslo verze — levný dotaz pro poll orchestrátoru."""
        stmt = select(setup_params_table.c.id).order_by(setup_params_table.c.id.desc()).limit(1)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        return int(row[0]) if row is not None else None

    def history(self, *, limit: int = 20) -> list[StoredParams]:
        """Verze od nejnovější; řádek s neplatným JSON se přeskočí a nezablokuje ostatní."""
        stmt = select(setup_params_table).order_by(setup_params_table.c.id.desc()).limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        result: list[StoredParams] = []
        for row in rows:
            created = row.created_ts
            if isinstance(created, dt.datetime) and created.tzinfo is None:
                created = created.replace(tzinfo=dt.UTC)
            try:
                params = params_from_dict(dict(row.params or {}))
            except ValueError:
                # Řádek z novější/starší verze kódu s neznámým klíčem — nelže se
                # výchozími hodnotami, ale ani se kvůli němu nezastaví čtení
                continue
            result.append(
                StoredParams(
                    version=int(row.id),
                    created_ts=created,
                    created_by=str(row.created_by),
                    note=str(row.note),
                    mechanics_version=int(row.mechanics_version),
                    params=params,
                )
            )
        return result
