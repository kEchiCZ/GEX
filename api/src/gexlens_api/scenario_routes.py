"""Scénář dne (#1173, #1126 bod 3a): založení se snímkem, seznam, obrázek, track record.

Scénář vzniká jen dopředu (rozhodnutí uživatele 15. 9. 2026): `created_at`
razítkuje server, `day` musí být aktuální seance, termín ≥ den vzniku; replay
minulého dne scénář nezaloží (422). Vyhodnocení dělá engine po settle
termínu (`ScenarioCollector`), API jen ukládá a čte.

Snímek: klient pošle PNG (base64) — složený canvas heatmapy + anotace —
uloží se do `data/scenarios/{sym}/{den}/{id}.png`; velikost snímků se
sčítá z DB (`image_bytes`), ne procházením stromu (#1105). Nad
`SCENARIO_DISK_LIMIT` odejde alert do zvonku (hranově, jednou za překročení)
— mazání je ruční (`DELETE /scenarios/images?older_than_days=N`), řádky
s výsledkem zůstávají.
"""

import base64
import datetime as dt
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from gexlens_engine.compute.scenario import PathPoint, targets_from_path
from gexlens_engine.compute.settle import settle_ts, trading_session_date
from gexlens_engine.storage.scenarios_store import ScenariosRepository

logger = logging.getLogger(__name__)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
IMAGE_MAX_BYTES = 4 * 1024 * 1024
#: Hlídání místa (#1173): nad 1 GB alert „pročisti", žádné automatické mazání
SCENARIO_DISK_LIMIT = 1024 * 1024 * 1024
MAX_TARGETS = 3
SCENARIOS_SUBDIR = "scenarios"


class ScenarioPathPoint(BaseModel):
    ts: dt.datetime
    price: float


class ScenarioImageIn(BaseModel):
    image_png_base64: str


class ScenarioIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    entry: float
    path: list[ScenarioPathPoint] = Field(min_length=1, max_length=2000)
    targets: list[float] | None = Field(default=None, max_length=MAX_TARGETS)
    deadline: dt.date | None = None
    annotation_id: int | None = None
    note: str | None = Field(default=None, max_length=1000)
    image_png_base64: str | None = None


def _decode_png(encoded: str | None) -> bytes | None:
    if not encoded:
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "Snímek není platný base64") from exc
    if not raw.startswith(PNG_MAGIC):
        raise HTTPException(422, "Snímek musí být PNG")
    if len(raw) > IMAGE_MAX_BYTES:
        raise HTTPException(422, f"Snímek přes {IMAGE_MAX_BYTES // (1024 * 1024)} MB")
    return raw


def build_scenario_router(
    repository_factory: Callable[[], ScenariosRepository],
    data_dir: Path,
    publish_alert: Any = None,
    *,
    now: Any = None,
) -> APIRouter:
    """`repository_factory` vrací úložiště nad sdíleným PG (lazy — DB nemusí být při
    sestavení app dostupná); `publish_alert(payload)` = LiveHub kanál alerts."""
    router = APIRouter(tags=["scenarios"])
    clock = now or (lambda: dt.datetime.now(dt.UTC))
    over_limit = {"value": False}
    # Schéma lazy při prvním dotazu (vzor sentiment routeru): app se sestavuje
    # i bez dostupné DB (testy, start před PG)
    cache: list[ScenariosRepository] = []

    def repo() -> ScenariosRepository:
        if not cache:
            repository = repository_factory()
            repository.ensure_schema()
            cache.append(repository)
        return cache[0]

    def _check_disk() -> dict[str, int]:
        usage = repo().disk_usage()
        over = usage["bytes"] >= SCENARIO_DISK_LIMIT
        if over and not over_limit["value"] and publish_alert is not None:
            publish_alert(
                {
                    "kind": "scenario_disk",
                    "symbol": "*",
                    "message": (
                        f"Snímky scénářů zabírají {usage['bytes'] / 1e9:.2f} GB "
                        f"({usage['images']} souborů) — pročisti v Settings → Scénáře"
                    ),
                    "ts": clock().timestamp(),
                }
            )
        over_limit["value"] = over
        return usage

    @router.post("/scenarios", status_code=201)
    def scenario_create(payload: ScenarioIn) -> dict[str, Any]:
        current = clock()
        day = trading_session_date(current)
        deadline = payload.deadline or day
        if deadline < day:
            raise HTTPException(422, "Termín scénáře nesmí být v minulosti")
        if (deadline - day).days > 60:
            raise HTTPException(422, "Termín scénáře nejvýš 60 dní dopředu")
        path = [
            PathPoint(
                ts=point.ts if point.ts.tzinfo else point.ts.replace(tzinfo=dt.UTC),
                price=point.price,
            )
            for point in payload.path
        ]
        targets = [float(value) for value in payload.targets] if payload.targets else []
        if not targets:
            targets = targets_from_path(path, payload.entry)
        if not targets:
            raise HTTPException(422, "Scénář nemá žádný cíl")
        image = _decode_png(payload.image_png_base64)
        scenario_id = repo().create(
            symbol=payload.symbol,
            day=day,
            created_at=current,
            deadline=deadline,
            deadline_ts=settle_ts(deadline),
            entry=payload.entry,
            targets=targets,
            path=[{"ts": point.ts.isoformat(), "price": point.price} for point in path],
            annotation_id=payload.annotation_id,
            note=payload.note,
        )
        if image is not None:
            relative = (
                Path(SCENARIOS_SUBDIR) / payload.symbol / day.isoformat() / f"{scenario_id}.png"
            )
            target = data_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(image)
            repo().set_image(scenario_id, relative.as_posix(), len(image))
            _check_disk()
        row = repo().get(scenario_id)
        assert row is not None
        return row.as_dict()

    @router.get("/scenarios")
    def scenarios_list(
        symbol: str | None = None,
        limit: int = Query(50, ge=1, le=500),
        source: str | None = Query(None, pattern="^(manual|auto)$"),
    ) -> dict[str, Any]:
        return {
            "scenarios": [
                row.as_dict() for row in repo().list_for(symbol, limit=limit, source=source)
            ]
        }

    @router.get("/scenarios/stats")
    def scenarios_stats(symbol: str | None = None) -> dict[str, Any]:
        """Track record scénářů per zdroj (auto = verdikt dne, manual = nakreslené);
        `preliminary` dokud n < 30 (stejná brána jako verdikt dne)."""
        total = repo().stats(symbol)
        by_source = {
            source: repo().stats(symbol, source=source) for source in ("auto", "manual")
        }
        return {
            "symbol": symbol,
            "preliminary": total["n"] < 30,
            **total,
            "by_source": {
                key: {**value, "preliminary": value["n"] < 30} for key, value in by_source.items()
            },
        }

    @router.put("/scenarios/{scenario_id}/image")
    def scenario_image_put(scenario_id: int, payload: ScenarioImageIn) -> dict[str, Any]:
        """Snímek k automatickému scénáři dodá frontend, jakmile má graf otevřený
        (#1173 A); ruční scénář ho má od založení. Jen jednou — druhý pokus 409."""
        row = repo().get(scenario_id)
        if row is None:
            raise HTTPException(404, f"Scénář {scenario_id} neexistuje")
        if row.image_path:
            raise HTTPException(409, "Scénář už snímek má")
        image = _decode_png(payload.image_png_base64)
        if image is None:
            raise HTTPException(422, "Chybí snímek")
        relative = Path(SCENARIOS_SUBDIR) / row.symbol / row.day.isoformat() / f"{scenario_id}.png"
        target = data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(image)
        repo().set_image(scenario_id, relative.as_posix(), len(image))
        _check_disk()
        updated = repo().get(scenario_id)
        assert updated is not None
        return updated.as_dict()

    @router.get("/scenarios/disk")
    def scenarios_disk() -> dict[str, Any]:
        usage = repo().disk_usage()
        return {
            **usage,
            "limit_bytes": SCENARIO_DISK_LIMIT,
            "over_limit": usage["bytes"] >= SCENARIO_DISK_LIMIT,
        }

    @router.delete("/scenarios/images")
    def scenarios_images_delete(older_than_days: int = Query(..., ge=1, le=3650)) -> dict[str, Any]:
        """Ruční úklid snímků starších než N dní — řádky s výsledky zůstávají."""
        cutoff = clock() - dt.timedelta(days=older_than_days)
        removed = 0
        freed = 0
        for row in repo().images_older_than(cutoff):
            if row.image_path:
                target = data_dir / row.image_path
                try:
                    if target.exists():
                        target.unlink()
                except OSError:
                    logger.exception("Snímek scénáře %s nešel smazat", target)
                    continue
            freed += row.image_bytes
            removed += 1
            repo().set_image(row.id, None, 0)
        over_limit["value"] = False
        return {"removed": removed, "freed_bytes": freed, **repo().disk_usage()}

    @router.get("/scenarios/{scenario_id}/image")
    def scenario_image(scenario_id: int) -> FileResponse:
        row = repo().get(scenario_id)
        if row is None or not row.image_path:
            raise HTTPException(404, "Snímek scénáře neexistuje")
        target = data_dir / row.image_path
        if not target.exists():
            raise HTTPException(404, "Snímek scénáře byl smazán")
        return FileResponse(target, media_type="image/png")

    @router.delete("/scenarios/{scenario_id}", status_code=204)
    def scenario_delete(scenario_id: int) -> None:
        row = repo().delete(scenario_id)
        if row is None:
            raise HTTPException(404, f"Scénář {scenario_id} neexistuje")
        if row.image_path:
            target = data_dir / row.image_path
            try:
                if target.exists():
                    target.unlink()
            except OSError:
                logger.exception("Snímek scénáře %s nešel smazat", target)

    return router
