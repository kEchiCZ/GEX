"""Sběr výskytů nezapnutých šablon (#577, mechanismus dle vzoru #256).

Tabulka `setup_probes` drží hypotetické obchody kandidátů (T9 „strop nad
hlavou" a zrcadlový výpad z pásma), vyhodnocované STEJNOU mechanikou jako
živé setupy (`evaluate_bar`/`r_result`) — bez toho by fáze 2 neměla co
porovnat s track recordem. Do `setups` ani track recordu se nezapisuje NIC:
nekalibrovaná šablona by zanesla jedinou věc, podle které se kalibruje
(#394). Kalibrační data — žádné delete API, retence se tabulky nedotýká.
"""

import datetime as dt
import logging
from typing import Any

from sqlalchemy import (
    JSON,
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

logger = logging.getLogger(__name__)

metadata = MetaData()

setup_probes = Table(
    "setup_probes",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("template", String(24), nullable=False),
    Column("symbol", String(16), nullable=False),
    Column("session_date", Date, nullable=False),
    Column("created_ts", DateTime(timezone=True), nullable=False),
    Column("direction", String(5), nullable=False),
    Column("entry", Float, nullable=False),
    Column("target", Float, nullable=False),
    Column("stop", Float, nullable=False),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("closed_ts", DateTime(timezone=True), nullable=True),
    Column("outcome_r", Float, nullable=True),
    Column("mfe", Float, nullable=True),
    Column("mae", Float, nullable=True),
    # Kontext výskytu (band metriky, geometrie zóny) — surovina fáze 2
    Column("context", JSON, nullable=False, default=dict),
)


class ProbeRepository:
    """Zápis a uzávěrky probe záznamů; čtení pro fázi 2 řeší SQL ručně."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        metadata.create_all(self._engine, tables=[setup_probes])

    def insert(
        self,
        *,
        template: str,
        symbol: str,
        session_date: dt.date,
        created_ts: dt.datetime,
        direction: str,
        entry: float,
        target: float,
        stop: float,
        context: dict[str, Any],
    ) -> int:
        with self._engine.begin() as conn:
            result = conn.execute(
                setup_probes.insert().values(
                    template=template,
                    symbol=symbol,
                    session_date=session_date,
                    created_ts=created_ts,
                    direction=direction,
                    entry=entry,
                    target=target,
                    stop=stop,
                    status="active",
                    context=context,
                )
            )
            key = result.inserted_primary_key
            assert key is not None  # autoincrement PK insert klíč vždy vrací
            return int(key[0])

    def close(
        self,
        probe_id: int,
        *,
        status: str,
        closed_ts: dt.datetime,
        outcome_r: float,
        mfe: float | None,
        mae: float | None,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(setup_probes)
                .where(setup_probes.c.id == probe_id)
                .values(status=status, closed_ts=closed_ts, outcome_r=outcome_r, mfe=mfe, mae=mae)
            )

    def count(self, symbol: str, template: str) -> int:
        """Počet výskytů — práh fáze 2 je ≥ 30 na instrument (#577)."""
        stmt = (
            select(func.count())
            .select_from(setup_probes)
            .where(setup_probes.c.symbol == symbol, setup_probes.c.template == template)
        )
        with self._engine.connect() as conn:
            return int(conn.execute(stmt).scalar_one())
