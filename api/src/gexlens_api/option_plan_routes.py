"""Plán opce QQQ ze scénáře dne (ADR-0062, F4a #1743): seznam a detail, jen čtení.

Plán zakládá a přeceňuje engine (`option_plan_job`), API tabulku `option_plans` jen čte
a nic nepočítá. Platný plán řádku vybírá `option_plans_store.effective_plan` (ADR-0062
K4 bod 10), táž funkce jako vyhodnocení (F3 #1748) a obrázek (F4b #1744).

Tvar odpovědi rozhodl vlastník 12. 11. (#1738, varianta B): seznam nese sloupce řádku
a platný plán, detail navíc celý uložený JSON `plan` (9:15, přecenění, pokrytí snímků).
Parametr `symbol` není (varianta A): tabulka ho nemá a plán je jen pro QQQ.
"""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from gexlens_engine.storage.option_plans_store import (
    OptionPlanRow,
    OptionPlansRepository,
    effective_plan,
)


def _summary(row: OptionPlanRow) -> dict[str, Any]:
    """Sloupce řádku a platný plán (`effective`); celý JSON `plan` jen v detailu."""
    return {
        "id": row.id,
        "scenario_id": row.scenario_id,
        "day": row.day.isoformat(),
        "created_at": row.created_at.isoformat(),
        "rules_version": row.rules_version,
        "status": row.status,
        "verdict": row.verdict,
        "evaluated_at": row.evaluated_at.isoformat() if row.evaluated_at else None,
        "result": row.result,
        "has_image": row.image_path is not None,
        "effective": effective_plan(row.plan),
    }


def build_option_plan_router(
    repository_factory: Callable[[], OptionPlansRepository],
) -> APIRouter:
    """`repository_factory` vrací úložiště nad sdíleným PG (lazy: app se sestavuje i bez DB)."""
    router = APIRouter(tags=["option-plans"])
    cache: list[OptionPlansRepository] = []

    def repo() -> OptionPlansRepository:
        if not cache:
            repository = repository_factory()
            # Tabulku zakládá engine (F2b #1742); tady jen pro start API před enginem a testy
            repository.ensure_schema()
            cache.append(repository)
        return cache[0]

    @router.get("/option-plans")
    def option_plans_list(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        """Nejnovější plány podle seance a `id`."""
        return {"option_plans": [_summary(row) for row in repo().list_plans(limit)]}

    @router.get("/option-plans/{plan_id}")
    def option_plan_detail(plan_id: int) -> dict[str, Any]:
        row = repo().get(plan_id)
        if row is None:
            raise HTTPException(404, f"Plán opce {plan_id} neexistuje")
        return {**_summary(row), "plan": row.plan}

    return router
