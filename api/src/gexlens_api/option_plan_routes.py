"""Plán opce QQQ ze scénáře dne (ADR-0062, F4a #1743): seznam a detail, jen čtení.

Plán zakládá a přeceňuje engine (`option_plan_job`), API tabulku `option_plans` jen čte
a nic nepočítá. Platný plán řádku vybírá `option_plans_store.effective_plan` (ADR-0062
K4 bod 10), táž funkce jako vyhodnocení (F3 #1748) a obrázek (F4b #1744).

Tvar odpovědi rozhodl vlastník 12. 11. (#1738, varianta B): seznam nese sloupce řádku
a platný plán, detail navíc celý uložený JSON `plan` (9:15, přecenění, pokrytí snímků).
Parametr `symbol` není (varianta A): tabulka ho nemá a plán je jen pro QQQ. Řádek nese
i odeslané přechody živého stavu `live_alerts` (F3b #1757), API je jen přepošle (F4d #1841).

Track record (F3 #1748, ADR-0062 bod 7): `GET /option-plans/stats` je tenký obal
`OptionPlansRepository.stats` podle vzoru `/scenarios/stats`: jen aktuální verze pravidel,
plány od `TRACK_RECORD_SINCE`, brána `preliminary` v každém bloku při n < 30.

PNG plánu (F4b #1744, ADR-0062 K1 A): `GET /option-plans/{id}/image` ho kreslí v krátkém
podprocesu (`plan_chart`) z uložených hodnot plánu a uložených barů QQQ a ukládá do
`data/scenarios/QQQ/{den}/plan-{id}-{stav}.png`. PNG je snímek stavu plánu (vlastník 12. 11.,
#1738, varianta A): nakreslí se při prvním dotazu a znovu jen po změně stavu
(`plan_chart.image_state`: 9:15, přecenění 9:31, vyhodnocení), jinak se čte z disku. Velikost
jde do `image_bytes` a do společného hlídání místa se snímky scénářů (#1173). Selhání kreslení
je 500 s důvodem v odpovědi i v logu, nikdy prázdný obrázek. Kreslení a uložení drží
`PlanImages`, který sdílí endpoint s fotkou plánu na Telegram (F4c #1745).
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
from gexlens_engine.compute.option_plan import RULES_VERSION, TRACK_RECORD_SINCE
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
    """Sloupce řádku a platný plán (`effective`); celý JSON `plan` jen v detailu.

    `live_alerts` jsou odeslané přechody živého stavu (F3b #1757) v pořadí zápisu: událost,
    čas, čas odeslání a příznak `final`; ceny a R:R nese text zprávy (Rozhodnuto 18. 11.
    F5a Q1 B, F4d #1841).
    """
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
        "live_alerts": list(row.live_alerts),
        "effective": effective_plan(row.plan),
    }


class PlanImageError(RuntimeError):
    """PNG plánu nejde vrátit; `status` je HTTP kód endpointu, zpráva nese důvod."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class PlanImages:
    """PNG plánu QQQ k jeho stavu: z disku, nebo nakreslený teď a uložený (F4b #1744).

    Jediná cesta k PNG plánu pro endpoint `GET /option-plans/{id}/image` i pro fotku
    na Telegram (F4c #1745): PNG ke stavu se nakreslí jednou, jde do `image_bytes`
    a do hlídání místa, ať se k němu dostane kdokoli dřív.

    `repository_factory` vrací úložiště nad sdíleným PG (lazy: app se sestavuje i bez DB),
    `data_dir` je kořen PNG (`data/scenarios/…`), `derived_dir` partice barů QQQ,
    `disk_watch` hlídání místa sdílené se snímky scénářů (None = bez hlídání, testy).
    """

    def __init__(
        self,
        repository_factory: Callable[[], OptionPlansRepository],
        data_dir: Path,
        derived_dir: Path,
        disk_watch: ImageDiskWatch | None = None,
        *,
        now: Any = None,
    ) -> None:
        self._factory = repository_factory
        self._data_dir = data_dir
        self._derived_dir = derived_dir
        self._disk_watch = disk_watch
        self._clock = now or (lambda: dt.datetime.now(dt.UTC))
        self._cache: list[OptionPlansRepository] = []
        # Kontrola „PNG ke stavu už je“ a kreslení jsou atomické: souběžné dotazy (endpoint,
        # Telegram) nekreslí dvakrát
        self._image_lock = threading.Lock()

    def repo(self) -> OptionPlansRepository:
        if not self._cache:
            repository = self._factory()
            # Tabulku zakládá engine (F2b #1742); tady jen pro start API před enginem a testy
            repository.ensure_schema()
            self._cache.append(repository)
        return self._cache[0]

    def path(self, plan_id: int) -> Path:
        """PNG plánu k jeho stavu; `PlanImageError` 404 bez plánu, 500 při selhání kreslení."""
        row = self.repo().get(plan_id)
        if row is None:
            raise PlanImageError(404, f"Plán opce {plan_id} neexistuje")
        target = self._stored(row)
        if target is None:
            with self._image_lock:
                row = self.repo().get(plan_id)
                if row is None:
                    raise PlanImageError(404, f"Plán opce {plan_id} neexistuje")
                target = self._stored(row) or self._draw(row)
        return target

    def _stored(self, row: OptionPlanRow) -> Path | None:
        """PNG ke stavu plánu na disku, jinak None (jiný stav, smazaný úklidem, nebyl)."""
        relative = image_relative_path(row)
        if row.image_path != relative.as_posix():
            return None
        target = self._data_dir / relative
        return target if target.exists() else None

    def _draw(self, row: OptionPlanRow) -> Path:
        relative = image_relative_path(row)
        try:
            bars = read_plan_bars(row, self._derived_dir, self._clock())
            png = render_png(plan_chart_spec(row, bars))
        except ChartRenderError as exc:
            logger.error(
                "PNG plánu QQQ #%s (stav %s) se nenakreslil: %s", row.id, image_state(row), exc
            )
            raise PlanImageError(500, f"Kreslení PNG plánu {row.id} selhalo: {exc}") from exc
        except Exception as exc:
            # Čtení barů nebo popis z JSON plánu (vadná partice, nečekaný tvar): důvod
            # do odpovědi i do logu s tracebackem, ne holé 500
            logger.exception("PNG plánu QQQ #%s: popis grafu selhal", row.id)
            reason = f"{type(exc).__name__}: {exc}"
            raise PlanImageError(500, f"Kreslení PNG plánu {row.id} selhalo: {reason}") from exc
        target = self._data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".tmp")
        partial.write_bytes(png)
        os.replace(partial, target)
        self.repo().set_image(row.id, relative.as_posix(), len(png))
        if row.image_path and row.image_path != relative.as_posix():
            # PNG předchozího stavu (9:15 po přecenění) je odvozený, ne data uživatele
            previous = self._data_dir / row.image_path
            try:
                previous.unlink(missing_ok=True)
            except OSError:
                logger.exception("PNG plánu QQQ %s nešel smazat", previous)
        if self._disk_watch is not None:
            self._disk_watch.check()
        logger.info(
            "PNG plánu QQQ #%s (stav %s): %d B, barů %d",
            row.id,
            image_state(row),
            len(png),
            len(bars),
        )
        return target


def build_option_plan_router(images: PlanImages) -> APIRouter:
    """Seznam, detail, track record a PNG plánu; PNG jde přes `images` sdílené s Telegramem."""
    router = APIRouter(tags=["option-plans"])
    repo = images.repo

    @router.get("/option-plans")
    def option_plans_list(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        """Nejnovější plány podle seance a `id`."""
        return {"option_plans": [_summary(row) for row in repo().list_plans(limit)]}

    @router.get("/option-plans/stats")
    def option_plans_stats() -> dict[str, Any]:
        """Track record plánu QQQ: hlavní plán a stíny K10 B zvlášť, obchody, vyřazené P3
        a třídy rozpočtu (Rozhodnuto 16. 11.). Bez data začátku je prázdný (`outside` = počet
        vyhodnocených plánů mimo něj). Registrace před `/{plan_id}`, jinak by `stats` byl id."""
        version = RULES_VERSION
        return {
            "rules_version": version,
            **repo().stats(rules_version=version, since=TRACK_RECORD_SINCE),
        }

    @router.get("/option-plans/{plan_id}/image")
    def option_plan_image(plan_id: int) -> FileResponse:
        """PNG plánu k jeho stavu: z disku, nebo nakreslený teď (snímek stavu plánu)."""
        try:
            target = images.path(plan_id)
        except PlanImageError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        return FileResponse(target, media_type="image/png")

    @router.get("/option-plans/{plan_id}")
    def option_plan_detail(plan_id: int) -> dict[str, Any]:
        row = repo().get(plan_id)
        if row is None:
            raise HTTPException(404, f"Plán opce {plan_id} neexistuje")
        return {**_summary(row), "plan": row.plan}

    return router
