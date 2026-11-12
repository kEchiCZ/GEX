"""Tabulka `option_plans` (ADR-0062 K2 A, F2b #1742) — plán opce QQQ ke scénáři dne NQ.

Jeden plán na auto-scénář (`scenario_id` je unikátní). Plán je point-in-time: řádek vznikne
jednou v 9:15 ET (K4 C) a JSON z 9:15 se zpětně nepřepisuje. Přecenění v 9:31 ET (F2c #1769)
přidá do JSON `plan` jen klíč `repricing`, jednou. `evaluated_at` a `result` doplní
vyhodnocení (F3 #1748), `image_path` PNG plánu (F4b #1744). Detail plánu nese JSON `plan`
(`compute/option_plan.plan_to_json`), sloupce slouží jen dotazům a vazbám a popisují aktuální
stav plánu (vlastník 11. 11., #1738):
- `status`: `sestaven` (z ranního snímku, nebo celý z 9:31 — pak `plan.repricing.plan`),
  jinak `bez opce` (důvody v `plan` a `plan.repricing`);
- `verdict`: hlavní slovo verdiktu hlavního plánu (`čeká`, `neobchodovat`, `obchodovat`),
  u plánu bez opce NULL.

Platný plán řádku (ADR-0062 K4 bod 10) vybírá jediná funkce `effective_plan`: čte ji API
(F4a #1743), vyhodnocení (F3 #1748) i obrázek (F4b #1744), pravidlo nemá kopii.

Vazba na `scenarios` není cizí klíč: scénář smí uživatel smazat a plán s výsledkem zůstává
(data se nemažou, AGENTS.md). PostgreSQL navždy, track record plánu.
"""

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    exists,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from gexlens_engine.storage.scenarios_store import scenarios_table

#: Klíč přecenění v 9:31 ET v JSON `plan` (F2c #1769); jeho existence = přecenění proběhlo
REPRICING_KEY = "repricing"

option_plans_metadata = MetaData()

option_plans_table = Table(
    "option_plans",
    option_plans_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # Auto-scénář NQ, ze kterého plán vznikl; jeden plán na scénář (restart bez duplicity)
    Column("scenario_id", Integer, nullable=False, unique=True),
    # Seance vzniku (trading session date) a okamžik plánu (štítek minuty scénáře, 9:15 ET)
    Column("day", Date, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # Verze pravidel plánu (`option_plan.RULES_VERSION`); změna pravidel = nová verze
    Column("rules_version", Integer, nullable=False),
    Column("verdict", String(16), nullable=True),
    Column("status", String(16), nullable=False),
    Column("plan", JSON, nullable=False),
    Column("evaluated_at", DateTime(timezone=True), nullable=True),
    Column("result", JSON, nullable=True),
    Column("image_path", String(255), nullable=True),
)


@dataclass(frozen=True)
class OptionPlanRow:
    id: int
    scenario_id: int
    day: dt.date
    created_at: dt.datetime
    rules_version: int
    verdict: str | None
    status: str
    plan: dict[str, Any]
    evaluated_at: dt.datetime | None
    result: dict[str, Any] | None
    image_path: str | None


def effective_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Platný plán řádku z JSON `plan` (ADR-0062 K4 bod 10); čistá funkce, nic nepočítá.

    - Plán sestavený celý v 9:31 ET (`plan.repricing.plan`) má přednost, `plan.main` je u něj
      null. Ceny z 9:31 nese sám (`main.variants`), `prices` je null.
    - Jinak platí `plan.main` a `plan.shadows` z 9:15 ET s cenami z 9:15 ve variantách
      a `prices` = ceny z 9:31 (`plan.repricing.prices`), když se přecenění zapsalo.

    `main` je null u plánu bez opce. `reasons` jsou mezery z 9:15 (`plan.reasons`),
    `repricing_reasons` mezery z 9:31; null = přecenění ještě neproběhlo.
    """
    repricing: Mapping[str, Any] | None = plan.get(REPRICING_KEY)
    rebuilt: Mapping[str, Any] | None = repricing.get("plan") if repricing is not None else None
    source = rebuilt if rebuilt is not None else plan
    return {
        "slot": source["slot"],
        "levels_nq": source["levels_nq"],
        "main": source["main"],
        "shadows": source["shadows"],
        "prices": repricing.get("prices") if repricing is not None and rebuilt is None else None,
        "reasons": list(plan["reasons"]),
        "repricing_reasons": list(repricing["reasons"]) if repricing is not None else None,
    }


def _utc(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite vrací naivní datetime (testy), PG nese zónu."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=dt.UTC)


def _row(mapping: Any) -> OptionPlanRow:
    created = _utc(mapping["created_at"])
    if created is None:
        raise ValueError(f"option_plans #{mapping['id']}: created_at chybí")
    return OptionPlanRow(
        id=int(mapping["id"]),
        scenario_id=int(mapping["scenario_id"]),
        day=mapping["day"],
        created_at=created,
        rules_version=int(mapping["rules_version"]),
        verdict=mapping["verdict"],
        status=str(mapping["status"]),
        plan=dict(mapping["plan"]),
        evaluated_at=_utc(mapping["evaluated_at"]),
        result=mapping["result"],
        image_path=mapping["image_path"],
    )


class OptionPlansRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_schema(self) -> None:
        option_plans_metadata.create_all(self._engine)

    def create(
        self,
        *,
        scenario_id: int,
        day: dt.date,
        created_at: dt.datetime,
        rules_version: int,
        verdict: str | None,
        status: str,
        plan: dict[str, Any],
    ) -> int | None:
        """Nový plán; None = plán ke scénáři už existuje a nic se nepřepíše (K2 A)."""
        try:
            with self._engine.begin() as conn:
                result = conn.execute(
                    insert(option_plans_table).values(
                        scenario_id=scenario_id,
                        day=day,
                        created_at=created_at,
                        rules_version=rules_version,
                        verdict=verdict,
                        status=status,
                        plan=plan,
                    )
                )
        except IntegrityError:
            if self.get_for_scenario(scenario_id) is None:
                raise  # jiné porušení než unikátní scenario_id
            return None
        key = result.inserted_primary_key
        if key is None:
            raise RuntimeError("insert option_plans nevrátil id")
        return int(key[0])

    def get(self, plan_id: int) -> OptionPlanRow | None:
        stmt = select(option_plans_table).where(option_plans_table.c.id == plan_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row(row) if row is not None else None

    def list_plans(self, limit: int) -> list[OptionPlanRow]:
        """Nejnovější plány: podle seance, v ní podle `id` (F4a #1743)."""
        stmt = (
            select(option_plans_table)
            .order_by(option_plans_table.c.day.desc(), option_plans_table.c.id.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_row(row) for row in rows]

    def get_for_scenario(self, scenario_id: int) -> OptionPlanRow | None:
        stmt = select(option_plans_table).where(option_plans_table.c.scenario_id == scenario_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return _row(row) if row is not None else None

    def plans_of_day(self, day: dt.date) -> list[OptionPlanRow]:
        """Plány seance podle `id` (jeden auto-scénář NQ za seanci = obvykle jeden plán)."""
        stmt = (
            select(option_plans_table)
            .where(option_plans_table.c.day == day)
            .order_by(option_plans_table.c.id)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_row(row) for row in rows]

    def record_repricing(
        self,
        plan_id: int,
        repricing: dict[str, Any],
        *,
        status: str | None = None,
        verdict: str | None = None,
    ) -> bool:
        """Přecenění v 9:31 ET (F2c #1769) jako klíč `plan.repricing`; False = už zapsané.

        Jednou za plán (restart nevytvoří druhé). Část JSON z 9:15 ET se nemění (K2 A).
        `status` a `verdict` přepíšou sloupce jen u plánu sestaveného celého v 9:31: sloupce
        popisují aktuální stav plánu (vlastník 11. 11., #1738); bez `status` zůstanou z 9:15.
        Řádek se v transakci zamkne (PG `FOR UPDATE`), kontrola a zápis jsou atomické.
        """
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    select(option_plans_table.c.plan)
                    .where(option_plans_table.c.id == plan_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if row is None:
                raise LookupError(f"option_plans #{plan_id} neexistuje")
            plan = dict(row["plan"])
            if REPRICING_KEY in plan:
                return False
            plan[REPRICING_KEY] = repricing
            values: dict[str, Any] = {"plan": plan}
            if status is not None:
                values |= {"status": status, "verdict": verdict}
            conn.execute(
                update(option_plans_table)
                .where(option_plans_table.c.id == plan_id)
                .values(**values)
            )
        return True

    def orphan_auto_scenario(self, symbol: str, day: dt.date) -> tuple[int, dt.datetime] | None:
        """(id, created_at) auto-scénáře seance bez plánu, jinak None — restart enginu (#1742)."""
        stmt = (
            select(scenarios_table.c.id, scenarios_table.c.created_at)
            .where(
                scenarios_table.c.symbol == symbol,
                scenarios_table.c.day == day,
                scenarios_table.c.source == "auto",
            )
            .where(~exists().where(option_plans_table.c.scenario_id == scenarios_table.c.id))
            .order_by(scenarios_table.c.id)
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None:
            return None
        created = _utc(row[1])
        if created is None:
            raise ValueError(f"scenarios #{row[0]}: created_at chybí")
        return int(row[0]), created
