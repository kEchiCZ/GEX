"""Plán opce QQQ ze scénáře dne (ADR-0062, F4a #1743): seznam a detail, jen čtení.

Plán zakládá a přeceňuje engine (`option_plan_job`), API tabulku `option_plans` jen čte
a nic nepočítá. Platný plán řádku vybírá `option_plans_store.effective_plan` (ADR-0062
K4 bod 10), táž funkce jako vyhodnocení (F3 #1748) a obrázek (F4b #1744).

Tvar odpovědi rozhodl vlastník 12. 11. (#1738, varianta B): seznam nese sloupce řádku
a platný plán, detail navíc celý uložený JSON `plan` (9:15, přecenění, pokrytí snímků).
Parametr `symbol` není (varianta A): tabulka ho nemá a plán je jen pro QQQ.

PNG plánu (F4b #1744, ADR-0062 K1 A): `GET /option-plans/{id}/image` ho kreslí v krátkém
podprocesu (`plan_chart`) z uložených hodnot plánu a uložených barů QQQ a ukládá do
`data/scenarios/QQQ/{den}/plan-{id}-{stav}.png`. PNG je snímek stavu plánu (vlastník 12. 11.,
#1738, varianta A): nakreslí se při prvním dotazu a znovu jen po změně stavu
(`plan_chart.image_state`: 9:15, přecenění 9:31, vyhodnocení), jinak se čte z disku. Velikost
jde do `image_bytes` a do společného hlídání místa se snímky scénářů (#1173). Selhání kreslení
je 500 s důvodem v odpovědi i v logu, nikdy prázdný obrázek.
"""

import datetime as dt
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from gexlens_api.plan_chart import (
    PLAN_SYMBOL,
    ChartRenderError,
    image_state,
    plan_chart_spec,
    read_plan_bars,
    render_png,
)
from gexlens_api.scenario_routes import SCENARIOS_SUBDIR, ImageDiskWatch
from gexlens_engine.storage.option_plans_store import (
    OptionPlanRow,
    OptionPlansRepository,
    effective_plan,
)

logger = logging.getLogger(__name__)


def image_relative_path(row: OptionPlanRow) -> Path:
    """Cesta PNG plánu vůči `data/` pro aktuální stav plánu (vedle snímků scénářů NQ)."""
    return (
        Path(SCENARIOS_SUBDIR)
        / PLAN_SYMBOL
        / row.day.isoformat()
        / f"plan-{row.id}-{image_state(row)}.png"
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
    data_dir: Path,
    derived_dir: Path,
    disk_watch: ImageDiskWatch | None = None,
    *,
    now: Any = None,
) -> APIRouter:
    """`repository_factory` vrací úložiště nad sdíleným PG (lazy: app se sestavuje i bez DB).

    `data_dir` je kořen PNG (`data/scenarios/…`), `derived_dir` partice barů QQQ,
    `disk_watch` hlídání místa sdílené se snímky scénářů (None = bez hlídání, testy).
    """
    router = APIRouter(tags=["option-plans"])
    clock = now or (lambda: dt.datetime.now(dt.UTC))
    cache: list[OptionPlansRepository] = []
    # Kontrola „PNG ke stavu už je“ a kreslení jsou atomické: souběžné dotazy nekreslí dvakrát
    image_lock = threading.Lock()

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

    def _stored_image(row: OptionPlanRow) -> Path | None:
        """PNG ke stavu plánu na disku, jinak None (jiný stav, smazaný úklidem, nebyl)."""
        relative = image_relative_path(row)
        if row.image_path != relative.as_posix():
            return None
        target = data_dir / relative
        return target if target.exists() else None

    def _draw(row: OptionPlanRow) -> Path:
        relative = image_relative_path(row)
        bars = read_plan_bars(row, derived_dir, clock())
        try:
            png = render_png(plan_chart_spec(row, bars))
        except ChartRenderError as exc:
            logger.error(
                "PNG plánu QQQ #%s (stav %s) se nenakreslil: %s", row.id, image_state(row), exc
            )
            raise HTTPException(500, f"Kreslení PNG plánu {row.id} selhalo: {exc}") from exc
        target = data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".tmp")
        partial.write_bytes(png)
        os.replace(partial, target)
        repo().set_image(row.id, relative.as_posix(), len(png))
        if row.image_path and row.image_path != relative.as_posix():
            # PNG předchozího stavu (9:15 po přecenění) je odvozený, ne data uživatele
            previous = data_dir / row.image_path
            try:
                previous.unlink(missing_ok=True)
            except OSError:
                logger.exception("PNG plánu QQQ %s nešel smazat", previous)
        if disk_watch is not None:
            disk_watch.check()
        logger.info(
            "PNG plánu QQQ #%s (stav %s): %d B, barů %d",
            row.id,
            image_state(row),
            len(png),
            len(bars),
        )
        return target

    @router.get("/option-plans/{plan_id}/image")
    def option_plan_image(plan_id: int) -> FileResponse:
        """PNG plánu k jeho stavu: z disku, nebo nakreslený teď (snímek stavu plánu)."""
        row = repo().get(plan_id)
        if row is None:
            raise HTTPException(404, f"Plán opce {plan_id} neexistuje")
        target = _stored_image(row)
        if target is None:
            with image_lock:
                row = repo().get(plan_id)
                if row is None:
                    raise HTTPException(404, f"Plán opce {plan_id} neexistuje")
                target = _stored_image(row) or _draw(row)
        return FileResponse(target, media_type="image/png")

    @router.get("/option-plans/{plan_id}")
    def option_plan_detail(plan_id: int) -> dict[str, Any]:
        row = repo().get(plan_id)
        if row is None:
            raise HTTPException(404, f"Plán opce {plan_id} neexistuje")
        return {**_summary(row), "plan": row.plan}

    return router
