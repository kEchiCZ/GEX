"""CRUD routy /watchlist, /alerts, /annotations, /settings (SPEC kap. 6, issue #21)."""

import datetime as dt
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from gexlens_api.alerts import AlertKind
from gexlens_api.meta_repo import DuplicateEntryError, MetaRepository, NotFoundError


class WatchlistItemIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)


class AlertIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    kind: AlertKind
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class AlertPatch(BaseModel):
    params: dict[str, Any] | None = None
    enabled: bool | None = None


class AnnotationIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=16)
    day: dt.date
    payload: dict[str, Any]


class SettingIn(BaseModel):
    value: Any


def build_router(repository: MetaRepository) -> APIRouter:
    router = APIRouter()

    @router.get("/watchlist")
    def watchlist_list() -> dict[str, list[dict[str, Any]]]:
        return {"watchlist": repository.watchlist()}

    @router.post("/watchlist", status_code=201)
    def watchlist_add(item: WatchlistItemIn) -> dict[str, Any]:
        try:
            return repository.watchlist_add(item.symbol)
        except DuplicateEntryError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.delete("/watchlist/{item_id}", status_code=204)
    def watchlist_remove(item_id: int) -> None:
        try:
            repository.watchlist_remove(item_id)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/alerts")
    def alerts_list() -> dict[str, list[dict[str, Any]]]:
        return {"alerts": repository.alerts()}

    @router.post("/alerts", status_code=201)
    def alert_create(alert: AlertIn) -> dict[str, Any]:
        return repository.alert_create(alert.symbol, alert.kind.value, alert.params, alert.enabled)

    @router.patch("/alerts/{alert_id}")
    def alert_update(alert_id: int, patch: AlertPatch) -> dict[str, Any]:
        fields = {name: value for name, value in patch.model_dump().items() if value is not None}
        if not fields:
            raise HTTPException(422, "Není co měnit (params/enabled)")
        try:
            return repository.alert_update(alert_id, **fields)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.delete("/alerts/{alert_id}", status_code=204)
    def alert_delete(alert_id: int) -> None:
        try:
            repository.alert_delete(alert_id)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/annotations")
    def annotations_list(symbol: str, date: dt.date) -> dict[str, list[dict[str, Any]]]:
        return {"annotations": repository.annotations(symbol, date)}

    @router.post("/annotations", status_code=201)
    def annotation_create(annotation: AnnotationIn) -> dict[str, Any]:
        return repository.annotation_create(annotation.symbol, annotation.day, annotation.payload)

    @router.delete("/annotations/{annotation_id}", status_code=204)
    def annotation_delete(annotation_id: int) -> None:
        try:
            repository.annotation_delete(annotation_id)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/settings")
    def settings_all() -> dict[str, Any]:
        return {"settings": repository.settings_all()}

    @router.put("/settings/{key}")
    def setting_put(key: str, setting: SettingIn) -> dict[str, Any]:
        repository.setting_put(key, setting.value)
        return {"key": key, "value": setting.value}

    return router
