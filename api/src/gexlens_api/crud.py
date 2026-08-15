"""CRUD routy /watchlist, /alerts, /annotations, /settings (SPEC kap. 6, issue #21)."""

import datetime as dt
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from gexlens_api.alerts import AlertKind
from gexlens_api.meta_repo import DuplicateEntryError, MetaRepository, NotFoundError
from gexlens_engine.runtime_settings import CONNECTION_SETTINGS, RUNTIME_SETTINGS
from gexlens_engine.storage.meta import (
    FAILURE_MODES,
    JOURNAL_GRADES,
    JOURNAL_PROFILES,
    JOURNAL_TYPES,
    MISTAKE_TAGS,
    PLAYBOOK_SCOPES,
    REPORT_GRADES,
    TRADE_DIRECTIONS,
    default_profile,
)

# Klíče řídící engine (#542 C4). Bez whitelistu byl `PUT /settings/{key}`
# neautentizované řízení enginu: `ibkr_host` ho přepojí na cizí server
# a `retention_days=1` nechá noční purge smazat archiv.
ENGINE_SETTINGS = frozenset(
    [spec.key for spec in RUNTIME_SETTINGS]
    + [spec.key for spec in CONNECTION_SETTINGS]
    + ["ibkr_host", "subscription_alert_enabled"]
)

# Předvolby UI držené na serveru, ať platí napříč prohlížeči. Engine je nečte,
# takže nemají bezpečnostní dopad — jen musí projít validací.
UI_SETTINGS = frozenset(["theme", "language", "sessions"])

# `retro_pass` chybí schválně — ten si news-engine píše přímo do DB, ne přes API.
WRITABLE_SETTINGS = ENGINE_SETTINGS | UI_SETTINGS


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


class AnnotationPut(BaseModel):
    """Nový payload existující anotace — přesun tažením (#589).

    Symbol ani den se nemění: anotace patří ke dni, ve kterém vznikla, a přesun
    ji posouvá jen v rámci jeho osy.
    """

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

    # ── Deník (#673 fáze A, #709 rev. 2) ───────────────────────────

    class JournalTrade(BaseModel):
        """Strukturovaný obchod. Plán i exekuce jsou volitelné — záznam smí
        vzniknout hned po vstupu a doplnit se po výstupu."""

        direction: str
        planned_entry: float | None = None
        planned_stop: float | None = None
        planned_target: float | None = None
        actual_entry: float | None = None
        actual_exit: float | None = None
        size: float | None = None
        opened_ts: dt.datetime | None = None
        closed_ts: dt.datetime | None = None
        setup_key: str | None = Field(None, max_length=64)
        failure_mode: str | None = None
        setup_grade: str | None = None
        execution_grade: str | None = None
        mistake_tags: list[str] = Field(default_factory=list, max_length=20)
        emotion: int | None = Field(None, ge=1, le=5)
        mfe: float | None = None
        mae: float | None = None
        gross_pnl: float | None = None
        net_pnl: float | None = None
        fees: float | None = None

    def _validated_trade(trade: JournalTrade) -> dict[str, Any]:
        if trade.direction not in TRADE_DIRECTIONS:
            raise HTTPException(422, f"direction musí být jeden z {TRADE_DIRECTIONS}")
        # Setup je povinný (#710): bez pojmenovaného setupu není co agregovat
        # — všechny statistiky z #714 na tom stojí.
        if not trade.setup_key:
            raise HTTPException(422, "Obchod musí mít setup z playbooku")
        known = repository.playbook_keys()
        if trade.setup_key not in known:
            raise HTTPException(422, f"Neznámý setup {trade.setup_key!r} (není v playbooku)")
        for label, grade in (
            ("setup_grade", trade.setup_grade),
            ("execution_grade", trade.execution_grade),
        ):
            if grade is not None and grade not in JOURNAL_GRADES:
                raise HTTPException(422, f"{label} musí být jeden z {JOURNAL_GRADES}")
        unknown = [tag for tag in trade.mistake_tags if tag not in MISTAKE_TAGS]
        if unknown:
            # Volný text by znemožnil spočítat, kolik která chyba stojí.
            raise HTTPException(422, f"Neznámé mistake tagy: {unknown}")
        if trade.failure_mode is not None and trade.failure_mode not in FAILURE_MODES:
            raise HTTPException(422, f"failure_mode musí být jeden z {FAILURE_MODES}")
        return trade.model_dump()

    def _validated_daily(daily: dict[str, Any]) -> dict[str, Any]:
        """Denní rituál (#712). Obsah je uživatelský dokument, kontroluje se
        jen to, co se pak vyhodnocuje strojově — známky segmentů."""
        review = daily.get("review")
        if isinstance(review, dict):
            segments = review.get("segments")
            if isinstance(segments, list):
                bad = [
                    segment.get("grade")
                    for segment in segments
                    if isinstance(segment, dict)
                    and segment.get("grade") not in (None, "", *REPORT_GRADES)
                ]
                if bad:
                    raise HTTPException(422, f"Známka segmentu musí být z {REPORT_GRADES}: {bad}")
        return daily

    @router.get("/journal")
    def journal_list(
        symbol: str | None = None,
        date: dt.date | None = None,
        entry_type: str | None = None,
        profile: str | None = None,
        limit: int = Query(500, ge=1, le=2000),
    ) -> dict[str, list[dict[str, Any]]]:
        if profile is not None and profile not in JOURNAL_PROFILES:
            raise HTTPException(422, f"profile musí být jeden z {JOURNAL_PROFILES}")
        return {
            "journal": repository.journal_list(
                symbol=symbol,
                day=date,
                entry_type=entry_type,
                profile=profile,
                limit=limit,
            )
        }

    @router.get("/journal/meta")
    def journal_meta() -> dict[str, Any]:
        """Číselníky pro UI — ať se výčty nedrží duplicitně v TS."""
        return {
            "types": list(JOURNAL_TYPES),
            "profiles": list(JOURNAL_PROFILES),
            "grades": list(JOURNAL_GRADES),
            "directions": list(TRADE_DIRECTIONS),
            "mistake_tags": list(MISTAKE_TAGS),
            "symbols": repository.journal_symbols(),
        }

    # ── PlayBook setupů (#710) ─────────────────────────────────────

    class PlaybookItem(BaseModel):
        """Karta setupu: podmínky vstupu, invalidace, management."""

        key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
        name: str = Field(min_length=1, max_length=128)
        profile: str = "both"
        thesis: str = Field("", max_length=4000)
        entry_conditions: str = Field("", max_length=4000)
        invalidation: str = Field("", max_length=4000)
        management: str = Field("", max_length=4000)

    class PlaybookPatch(BaseModel):
        name: str | None = Field(None, min_length=1, max_length=128)
        profile: str | None = None
        thesis: str | None = Field(None, max_length=4000)
        entry_conditions: str | None = Field(None, max_length=4000)
        invalidation: str | None = Field(None, max_length=4000)
        management: str | None = Field(None, max_length=4000)
        active: bool | None = None

    @router.get("/playbook")
    def playbook_list(include_inactive: bool = False) -> dict[str, list[dict[str, Any]]]:
        return {"playbook": repository.playbook(include_inactive=include_inactive)}

    @router.post("/playbook", status_code=201)
    def playbook_create(item: PlaybookItem) -> dict[str, Any]:
        if item.profile not in PLAYBOOK_SCOPES:
            raise HTTPException(422, f"profile musí být jeden z {PLAYBOOK_SCOPES}")
        try:
            return repository.playbook_create(
                {**item.model_dump(), "active": True, "created_ts": dt.datetime.now(dt.UTC)}
            )
        except DuplicateEntryError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.patch("/playbook/{item_id}")
    def playbook_update(item_id: int, patch: PlaybookPatch) -> dict[str, Any]:
        fields = {name: value for name, value in patch.model_dump().items() if value is not None}
        if not fields:
            raise HTTPException(422, "Není co měnit")
        if "profile" in fields and fields["profile"] not in PLAYBOOK_SCOPES:
            raise HTTPException(422, f"profile musí být jeden z {PLAYBOOK_SCOPES}")
        fields["updated_ts"] = dt.datetime.now(dt.UTC)
        try:
            # Vyřazení je `active=false`, ne DELETE — historické záznamy
            # na setup odkazují a musí zůstat čitelné.
            return repository.playbook_update(item_id, fields)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    class JournalEntry(BaseModel):
        """Nový záznam deníku; ts_ref = okamžik, ke kterému se vztahuje."""

        ts_ref: dt.datetime
        symbol: str = Field(min_length=1, max_length=12, pattern=r"^[A-Z0-9.]+$")
        entry_type: str
        text: str = Field(min_length=1, max_length=10_000)
        tags: list[str] = Field(default_factory=list, max_length=20)
        setup_id: int | None = None
        news_event_id: int | None = None
        profile: str | None = None
        trade: JournalTrade | None = None
        # Snímek GEX kontextu k ts_ref (#711) — skládá klient, server ho jen
        # uloží tak, jak přišel; přepočet na serveru by dal jiná čísla.
        context: dict[str, Any] | None = None
        # Ranní plán / Daily Report Card (#712) — jen u typu `retro_dne`.
        daily: dict[str, Any] | None = None

    class JournalPatch(BaseModel):
        """Úprava záznamu — text/tagy/profil a obchod; okamžik ani typ se
        zpětně nemění (jinak by značka ✎ ukazovala na jinou minutu)."""

        text: str | None = Field(None, min_length=1, max_length=10_000)
        tags: list[str] | None = Field(None, max_length=20)
        profile: str | None = None
        trade: JournalTrade | None = None
        daily: dict[str, Any] | None = None

    @router.post("/journal", status_code=201)
    def journal_create(entry: JournalEntry) -> dict[str, Any]:
        if entry.entry_type not in JOURNAL_TYPES:
            raise HTTPException(422, f"entry_type musí být jeden z {JOURNAL_TYPES}")
        profile = entry.profile or default_profile(entry.symbol)
        if profile not in JOURNAL_PROFILES:
            raise HTTPException(422, f"profile musí být jeden z {JOURNAL_PROFILES}")
        if entry.entry_type == "obchod" and entry.trade is None:
            raise HTTPException(422, "Typ 'obchod' vyžaduje objekt trade")
        if entry.entry_type != "obchod" and entry.trade is not None:
            raise HTTPException(422, "Objekt trade patří jen k typu 'obchod'")
        if entry.daily is not None and entry.entry_type != "retro_dne":
            raise HTTPException(422, "Denní rituál patří jen k typu 'retro_dne'")
        trade = _validated_trade(entry.trade) if entry.trade is not None else None
        daily = _validated_daily(entry.daily) if entry.daily is not None else None
        now = dt.datetime.now(dt.UTC)
        return repository.journal_create(
            {
                "ts_ref": entry.ts_ref,
                "symbol": entry.symbol,
                "entry_type": entry.entry_type,
                "text": entry.text,
                "tags": entry.tags,
                "setup_id": entry.setup_id,
                "news_event_id": entry.news_event_id,
                "profile": profile,
                "context": entry.context,
                "daily": daily,
                "created_ts": now,
            },
            trade,
        )

    @router.patch("/journal/{entry_id}")
    def journal_update(entry_id: int, patch: JournalPatch) -> dict[str, Any]:
        values: dict[str, Any] = {"updated_ts": dt.datetime.now(dt.UTC)}
        if patch.text is not None:
            values["text"] = patch.text
        if patch.tags is not None:
            values["tags"] = patch.tags
        if patch.profile is not None:
            if patch.profile not in JOURNAL_PROFILES:
                raise HTTPException(422, f"profile musí být jeden z {JOURNAL_PROFILES}")
            values["profile"] = patch.profile
        if patch.daily is not None:
            values["daily"] = _validated_daily(patch.daily)
        trade = _validated_trade(patch.trade) if patch.trade is not None else None
        if len(values) == 1 and trade is None:
            raise HTTPException(422, "Úprava musí měnit text, tagy, profil, obchod nebo rituál")
        try:
            return repository.journal_update(entry_id, values, trade)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.delete("/journal/{entry_id}", status_code=204)
    def journal_delete(entry_id: int) -> None:
        try:
            repository.journal_delete(entry_id)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/annotations")
    def annotations_list(symbol: str, date: dt.date) -> dict[str, list[dict[str, Any]]]:
        return {"annotations": repository.annotations(symbol, date)}

    @router.post("/annotations", status_code=201)
    def annotation_create(annotation: AnnotationIn) -> dict[str, Any]:
        return repository.annotation_create(annotation.symbol, annotation.day, annotation.payload)

    @router.put("/annotations/{annotation_id}")
    def annotation_put(annotation_id: int, annotation: AnnotationPut) -> dict[str, Any]:
        try:
            return repository.annotation_update(annotation_id, annotation.payload)
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

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
        if key not in WRITABLE_SETTINGS:
            raise HTTPException(
                422,
                f"Neznámý klíč nastavení: {key!r} "
                f"(povolené: {', '.join(sorted(WRITABLE_SETTINGS))})",
            )
        repository.setting_put(key, setting.value)
        return {"key": key, "value": setting.value}

    return router
