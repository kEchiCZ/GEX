"""Tabulka `scenarios` (#1173) — scénář dne se snímkem, cíli, termínem a výsledkem.

Vlastní metadata po vzoru gamma_cliff. PostgreSQL navždy: výsledky jsou
track record (hit rate cílů, odchylka od cesty), snímek PNG leží na disku
(`data/scenarios/{sym}/{den}/{id}.png`) a po ručním úklidu (#1173: hlídání
1 GB) může chybět — řádek s výsledkem zůstává (`image_path` NULL).
Vzniká jen dopředu: `created_at` razítkuje server, `deadline` ≥ den vzniku.
"""

import datetime as dt
from dataclasses import dataclass
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
    Text,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

scenarios_metadata = MetaData()

scenarios_table = Table(
    "scenarios",
    scenarios_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String(16), nullable=False),
    # Seance vzniku (trading session date) a okamžik vzniku
    Column("day", Date, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # Termín: seance, do jejíhož settle se scénář hodnotí (≥ day)
    Column("deadline", Date, nullable=False),
    Column("deadline_ts", DateTime(timezone=True), nullable=False),
    # Vstupní cena (spot při vzniku) a cíle v pořadí
    Column("entry", Float, nullable=False),
    Column("targets", JSON, nullable=False),
    # Cesta scénáře [{ts, price}] — z anotace, ať vyhodnocení nezávisí na jejím smazání
    Column("path", JSON, nullable=False),
    Column("annotation_id", Integer, nullable=True),
    Column("note", Text, nullable=True),
    Column("image_path", String(255), nullable=True),
    Column("image_bytes", Integer, nullable=False, default=0),
    Column("evaluated_at", DateTime(timezone=True), nullable=True),
    Column("result", JSON, nullable=True),
)


@dataclass(frozen=True)
class ScenarioRow:
    id: int
    symbol: str
    day: dt.date
    created_at: dt.datetime
    deadline: dt.date
    deadline_ts: dt.datetime
    entry: float
    targets: list[float]
    path: list[dict[str, Any]]
    annotation_id: int | None
    note: str | None
    image_path: str | None
    image_bytes: int
    evaluated_at: dt.datetime | None
    result: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "day": self.day.isoformat(),
            "created_at": self.created_at.isoformat(),
            "deadline": self.deadline.isoformat(),
            "deadline_ts": self.deadline_ts.isoformat(),
            "entry": self.entry,
            "targets": self.targets,
            "path": self.path,
            "annotation_id": self.annotation_id,
            "note": self.note,
            "has_image": self.image_path is not None,
            "image_bytes": self.image_bytes,
            "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
            "result": self.result,
        }


def _row(mapping: Any) -> ScenarioRow:
    created = mapping["created_at"]
    deadline_ts = mapping["deadline_ts"]
    evaluated = mapping["evaluated_at"]
    # SQLite vrací naivní datetime — testy; PG nese zónu
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.UTC)
    if deadline_ts.tzinfo is None:
        deadline_ts = deadline_ts.replace(tzinfo=dt.UTC)
    if evaluated is not None and evaluated.tzinfo is None:
        evaluated = evaluated.replace(tzinfo=dt.UTC)
    return ScenarioRow(
        id=int(mapping["id"]),
        symbol=str(mapping["symbol"]),
        day=mapping["day"],
        created_at=created,
        deadline=mapping["deadline"],
        deadline_ts=deadline_ts,
        entry=float(mapping["entry"]),
        targets=[float(value) for value in (mapping["targets"] or [])],
        path=list(mapping["path"] or []),
        annotation_id=mapping["annotation_id"],
        note=mapping["note"],
        image_path=mapping["image_path"],
        image_bytes=int(mapping["image_bytes"] or 0),
        evaluated_at=evaluated,
        result=mapping["result"],
    )


class ScenariosRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        scenarios_metadata.create_all(self._engine)

    def create(
        self,
        *,
        symbol: str,
        day: dt.date,
        created_at: dt.datetime,
        deadline: dt.date,
        deadline_ts: dt.datetime,
        entry: float,
        targets: list[float],
        path: list[dict[str, Any]],
        annotation_id: int | None,
        note: str | None,
    ) -> int:
        with self._engine.begin() as conn:
            result = conn.execute(
                insert(scenarios_table).values(
                    symbol=symbol,
                    day=day,
                    created_at=created_at,
                    deadline=deadline,
                    deadline_ts=deadline_ts,
                    entry=entry,
                    targets=targets,
                    path=path,
                    annotation_id=annotation_id,
                    note=note,
                    image_path=None,
                    image_bytes=0,
                )
            )
            key = result.inserted_primary_key
            if key is None:
                raise RuntimeError("insert scenarios nevrátil id")
            return int(key[0])

    def set_image(self, scenario_id: int, image_path: str | None, image_bytes: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(scenarios_table)
                .where(scenarios_table.c.id == scenario_id)
                .values(image_path=image_path, image_bytes=image_bytes)
            )

    def get(self, scenario_id: int) -> ScenarioRow | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(select(scenarios_table).where(scenarios_table.c.id == scenario_id))
                .mappings()
                .first()
            )
        return _row(row) if row is not None else None

    def list_for(self, symbol: str | None, *, limit: int = 50) -> list[ScenarioRow]:
        stmt = select(scenarios_table).order_by(scenarios_table.c.created_at.desc()).limit(limit)
        if symbol:
            stmt = stmt.where(scenarios_table.c.symbol == symbol)
        with self._engine.connect() as conn:
            return [_row(row) for row in conn.execute(stmt).mappings()]

    def pending(self, symbol: str, now: dt.datetime) -> list[ScenarioRow]:
        """Scénáře po termínu bez výsledku — vstup večerního vyhodnocení."""
        stmt = (
            select(scenarios_table)
            .where(scenarios_table.c.symbol == symbol)
            .where(scenarios_table.c.evaluated_at.is_(None))
            .where(scenarios_table.c.deadline_ts <= now)
            .order_by(scenarios_table.c.created_at)
        )
        with self._engine.connect() as conn:
            return [_row(row) for row in conn.execute(stmt).mappings()]

    def record_result(
        self, scenario_id: int, result: dict[str, Any] | None, evaluated_at: dt.datetime
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(scenarios_table)
                .where(scenarios_table.c.id == scenario_id)
                .values(result=result, evaluated_at=evaluated_at)
            )

    def delete(self, scenario_id: int) -> ScenarioRow | None:
        row = self.get(scenario_id)
        if row is None:
            return None
        with self._engine.begin() as conn:
            conn.execute(delete(scenarios_table).where(scenarios_table.c.id == scenario_id))
        return row

    def images_older_than(self, cutoff: dt.datetime) -> list[ScenarioRow]:
        stmt = (
            select(scenarios_table)
            .where(scenarios_table.c.image_path.is_not(None))
            .where(scenarios_table.c.created_at < cutoff)
        )
        with self._engine.connect() as conn:
            return [_row(row) for row in conn.execute(stmt).mappings()]

    def disk_usage(self) -> dict[str, int]:
        """Součet velikostí snímků z DB — bez procházení stromu (#1105 lekce)."""
        stmt = select(
            func.coalesce(func.sum(scenarios_table.c.image_bytes), 0),
            func.count(scenarios_table.c.id),
            func.count(scenarios_table.c.image_path),
        )
        with self._engine.connect() as conn:
            total, rows, images = conn.execute(stmt).one()
        return {"bytes": int(total), "scenarios": int(rows), "images": int(images)}

    def stats(self, symbol: str | None) -> dict[str, Any]:
        """Track record vyhodnocených scénářů: hit rate cílů, pořadí, medián odchylky."""
        stmt = select(scenarios_table).where(scenarios_table.c.result.is_not(None))
        if symbol:
            stmt = stmt.where(scenarios_table.c.symbol == symbol)
        with self._engine.connect() as conn:
            rows = [_row(row) for row in conn.execute(stmt).mappings()]
        results = [row.result for row in rows if row.result]
        n = len(results)
        hit1 = sum(1 for r in results if r.get("hit1"))
        with_second = [r for r in results if r.get("hit2") is not None]
        hit2 = sum(1 for r in with_second if r.get("hit2"))
        ordered = [r for r in results if r.get("order_ok") is not None]
        order_ok = sum(1 for r in ordered if r.get("order_ok"))
        devs = sorted(float(r["max_dev_em"]) for r in results if r.get("max_dev_em") is not None)
        median_dev = devs[len(devs) // 2] if devs else None
        return {
            "n": n,
            "hit1": hit1,
            "hit1_rate": hit1 / n if n else None,
            "n_second": len(with_second),
            "hit2": hit2,
            "hit2_rate": hit2 / len(with_second) if with_second else None,
            "n_order": len(ordered),
            "order_ok": order_ok,
            "order_rate": order_ok / len(ordered) if ordered else None,
            "median_dev_em": median_dev,
        }
