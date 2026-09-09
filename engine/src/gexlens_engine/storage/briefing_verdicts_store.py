"""Úložiště výsledků verdiktů dne (#1091, ADR-0035 §3).

Verdikt zapisuje frontend přes API (`briefing_verdicts`, meta schéma); engine
sem po settle doplní, jak seance skutečně dopadla, a API z toho počítá
track record. Sloupce výsledku jsou aditivní (`ensure_schema` je doplní
ALTERem do tabulky založené před #1091), nikdy se nemažou ani nepřepisují
verdikt samotný — predikce zůstává neměnná (stejná zásada jako u setupů).
"""

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import inspect, select, text, update
from sqlalchemy.engine import Engine

from gexlens_engine.compute.setupstats import wilson_lower_bound
from gexlens_engine.storage.meta import briefing_verdicts_table, ensure_meta_schema

#: Aditivní sloupce výsledku seance (#1091) — název → SQL typ
OUTCOME_COLUMNS: dict[str, str] = {
    "outcome_open": "FLOAT",
    "outcome_us_open": "FLOAT",
    "outcome_close": "FLOAT",
    "outcome_move_pts": "FLOAT",
    "outcome_move_em": "FLOAT",
    "outcome_hit": "BOOLEAN",
    "outcome_computed_at": "TIMESTAMP",
}

#: Brána vzorku jako u signálů (SPEC 6.2): pod ní se hit-rate nevykládá
STATS_MIN_SAMPLES = 30


@dataclass(frozen=True)
class VerdictOutcome:
    """Výsledek seance k verdiktu — `hit` None = nehodnotí se (wait_news, bez EM)."""

    open: float
    us_open: float | None
    close: float
    move_pts: float
    move_em: float | None
    hit: bool | None


class BriefingVerdictRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        """Meta schéma + aditivní sloupce výsledku (vzor journal, ADR bez alembicu)."""
        ensure_meta_schema(self._engine)
        inspector = inspect(self._engine)
        table = briefing_verdicts_table.name
        if not inspector.has_table(table):
            return
        columns = {col["name"] for col in inspector.get_columns(table)}
        missing = {name: sql for name, sql in OUTCOME_COLUMNS.items() if name not in columns}
        if not missing:
            return
        with self._engine.begin() as conn:
            for name, sql_type in missing.items():
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))

    def pending(self, symbol: str, *, before: dt.date) -> list[dict[str, Any]]:
        """Verdikty seancí před `before` bez doplněného výsledku (nejstarší první)."""
        stmt = (
            select(briefing_verdicts_table)
            .where(
                briefing_verdicts_table.c.symbol == symbol,
                briefing_verdicts_table.c.session_date < before,
                briefing_verdicts_table.c.outcome_computed_at.is_(None),
            )
            .order_by(briefing_verdicts_table.c.session_date)
        )
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]

    def update_outcome(self, verdict_id: int, outcome: VerdictOutcome, now: dt.datetime) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(briefing_verdicts_table)
                .where(briefing_verdicts_table.c.id == verdict_id)
                .values(
                    outcome_open=outcome.open,
                    outcome_us_open=outcome.us_open,
                    outcome_close=outcome.close,
                    outcome_move_pts=outcome.move_pts,
                    outcome_move_em=outcome.move_em,
                    outcome_hit=outcome.hit,
                    outcome_computed_at=now,
                )
            )

    def evaluated(self, symbol: str | None, *, since: dt.date) -> list[dict[str, Any]]:
        """Verdikty s výsledkem od `since` (podklad statistik)."""
        stmt = (
            select(briefing_verdicts_table)
            .where(
                briefing_verdicts_table.c.session_date >= since,
                briefing_verdicts_table.c.outcome_computed_at.is_not(None),
            )
            .order_by(briefing_verdicts_table.c.session_date)
        )
        if symbol is not None:
            stmt = stmt.where(briefing_verdicts_table.c.symbol == symbol)
        with self._engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(stmt)]


def verdict_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Track record verdiktů: per verdikt a per složka hlasování (#1091).

    Složka „nese informaci", když její nenulový hlas souhlasí se směrem seance
    (hlas > 0 a pohyb nahoru, hlas < 0 a dolů). Hodnotí se jen seance
    s nenulovým pohybem; Wilsonova dolní mez a brána n ≥ STATS_MIN_SAMPLES
    jako u signálů (SPEC 6.2).
    """
    by_verdict: dict[str, dict[str, int]] = {}
    by_vote: dict[str, dict[str, int]] = {}
    evaluated = 0
    for row in rows:
        verdict = str(row.get("verdict"))
        hit = row.get("outcome_hit")
        bucket = by_verdict.setdefault(verdict, {"n": 0, "hits": 0, "unscored": 0})
        if hit is None:
            bucket["unscored"] += 1
        else:
            bucket["n"] += 1
            bucket["hits"] += int(bool(hit))
            evaluated += 1
        move = row.get("outcome_move_pts")
        if not isinstance(move, int | float) or move == 0:
            continue
        for vote in row.get("votes") or []:
            value = vote.get("vote") if isinstance(vote, dict) else None
            name = vote.get("name") if isinstance(vote, dict) else None
            if not isinstance(value, int | float) or value == 0 or not isinstance(name, str):
                continue
            entry = by_vote.setdefault(name, {"n": 0, "hits": 0})
            entry["n"] += 1
            entry["hits"] += int((value > 0) == (move > 0))

    def finish(entry: dict[str, int]) -> dict[str, Any]:
        n = entry["n"]
        hits = entry["hits"]
        return {
            **entry,
            "hit_rate": hits / n if n else None,
            "wilson_lb": wilson_lower_bound(hits, n) if n else None,
            "gate_open": n >= STATS_MIN_SAMPLES,
        }

    return {
        "evaluated": evaluated,
        "min_samples": STATS_MIN_SAMPLES,
        "by_verdict": {key: finish(value) for key, value in sorted(by_verdict.items())},
        "by_vote": {key: finish(value) for key, value in sorted(by_vote.items())},
    }
