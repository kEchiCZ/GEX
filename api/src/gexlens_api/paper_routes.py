"""Paper účet (#1187 fáze 1, ADR-0040): účet, ordery, kill switch, události.

Risk vrstva tady **blokuje** (rozhodnutí uživatele 16. 9. 2026): order nad
rozpočtem rizika (#1185: účet × risk_pct, tvrdý strop risk_max_pct), po denní
/týdenní brzdě nebo při zapnutém kill switchi se nepodá — 409 s důvodem.
Fily dělá engine (`gexlens_engine.paper`), API jen zapisuje záměr a čte stav.
"""

import datetime as dt
from collections.abc import Callable
from typing import Any, cast

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from gexlens_engine.compute.paper import POINT_VALUES, OrderType, Side, validate_levels
from gexlens_engine.compute.risk import brake_state, position_size, week_start
from gexlens_engine.compute.settle import trading_session_date
from gexlens_engine.compute.setups import SetupParams
from gexlens_engine.storage.paper_store import DEFAULT_ACCOUNT_ID, PaperRepository

SIDES = ("long", "short")
ORDER_TYPES = ("market", "limit", "stop")
EVENT_KINDS = ("deposit", "withdraw", "note")


class OrderIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=12, pattern=r"^[A-Z0-9.]+$")
    side: str
    qty: int = Field(ge=1, le=100)
    order_type: str = "limit"
    #: U market orderu = referenční cena pro sizing (aktuální spot z UI)
    entry_price: float
    stop_price: float
    target_price: float | None = None
    setup_key: str | None = Field(default=None, max_length=64)
    setup_id: int | None = None
    note: str | None = Field(default=None, max_length=500)
    context: dict[str, Any] = Field(default_factory=dict)


class EventIn(BaseModel):
    kind: str
    amount: float = Field(default=0.0, ge=0)
    note: str | None = Field(default=None, max_length=500)


class KillIn(BaseModel):
    reason: str = Field(default="kill switch", max_length=200)


def build_paper_router(
    repository_factory: Callable[[], PaperRepository],
    params_factory: Callable[[], SetupParams],
    publish_alert: Callable[[dict[str, Any]], Any],
    *,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> APIRouter:
    router = APIRouter(prefix="/paper", tags=["paper"])

    def _brakes(repo: PaperRepository, params: SetupParams, moment: dt.datetime) -> Any:
        session_day = trading_session_date(moment)
        realized = repo.realized_since(week_start(session_day))
        return brake_state(
            realized,
            "paper",
            session_day=session_day,
            daily_brake_r=params.daily_brake_r,
            weekly_brake_r=params.weekly_brake_r,
            max_template_stops_per_day=0,  # strop stopů šablony u ručních orderů neplatí
        )

    def _account_payload(
        repo: PaperRepository, params: SetupParams, moment: dt.datetime
    ) -> dict[str, Any]:
        account = repo.account()
        if account is None:
            raise HTTPException(503, "Paper účet není založen (engine ho zakládá při startu)")
        equity = repo.equity()
        brakes = _brakes(repo, params, moment)
        return {
            "id": account.id,
            "name": account.name,
            "equity_start": account.equity_start,
            "equity": equity,
            "halted": account.halted,
            "halted_reason": account.halted_reason,
            "risk_pct": params.risk_pct,
            "risk_max_pct": params.risk_max_pct,
            "risk_budget_usd": equity * params.risk_pct / 100.0,
            "day_r": brakes.day_r,
            "week_r": brakes.week_r,
            "brake": brakes.block,
            "daily_brake_r": params.daily_brake_r,
            "weekly_brake_r": params.weekly_brake_r,
            "open": [o for o in repo.list_orders(status="open")],
            "working": [o for o in repo.list_orders(status="working")],
        }

    @router.get("/account")
    def account() -> dict[str, Any]:
        return _account_payload(repository_factory(), params_factory(), now())

    @router.get("/orders")
    def orders(
        symbol: str | None = None, status: str | None = None, limit: int = 200
    ) -> dict[str, Any]:
        repo = repository_factory()
        return {
            "orders": repo.list_orders(symbol=symbol, status=status, limit=max(1, min(limit, 1000)))
        }

    @router.post("/orders", status_code=201)
    def place(body: OrderIn) -> dict[str, Any]:
        if body.side not in SIDES:
            raise HTTPException(422, f"side musí být jeden z {SIDES}")
        if body.order_type not in ORDER_TYPES:
            raise HTTPException(422, f"order_type musí být jeden z {ORDER_TYPES}")
        point_value = POINT_VALUES.get(body.symbol)
        if point_value is None:
            raise HTTPException(422, f"Neznámá hodnota bodu pro {body.symbol}")
        error = validate_levels(
            cast(Side, body.side),
            cast(OrderType, body.order_type),
            body.entry_price,
            body.stop_price,
            body.target_price,
        )
        if error:
            raise HTTPException(422, error)
        repo = repository_factory()
        params = params_factory()
        moment = now()
        account = repo.account()
        if account is None:
            raise HTTPException(503, "Paper účet není založen")
        if account.halted:
            raise HTTPException(
                409, {"block": "kill_switch", "reason": account.halted_reason or "kill switch"}
            )
        if repo.active(body.symbol):
            raise HTTPException(
                409,
                {
                    "block": "position_exists",
                    "reason": f"{body.symbol} už má čekající nebo otevřený order",
                },
            )
        brakes = _brakes(repo, params, moment)
        if brakes.block is not None:
            raise HTTPException(
                409,
                {
                    "block": brakes.block,
                    "reason": f"brzda: dnes {brakes.day_r:+.1f} R, týden {brakes.week_r:+.1f} R",
                },
            )
        equity = repo.equity()
        size = position_size(
            body.entry_price,
            body.stop_price,
            point_value,
            account_equity_usd=equity,
            risk_pct=params.risk_pct,
            risk_max_pct=params.risk_max_pct,
        )
        per_contract = size.stop_points * point_value
        max_loss = per_contract * body.qty
        if not size.affordable or body.qty > size.contracts:
            raise HTTPException(
                409,
                {
                    "block": size.block or "stop_over_budget",
                    "reason": (
                        f"ztráta na stopu {max_loss:.0f} $ ({body.qty}× {size.stop_points:g} b) "
                        f"přesahuje rozpočet {size.risk_budget_usd:.0f} $"
                        + (f"; max {size.contracts} ks" if size.contracts > 0 else "; zkrať stop")
                    ),
                    "max_contracts": size.contracts,
                    "risk_budget_usd": size.risk_budget_usd,
                },
            )
        order_id = repo.create_order(
            {
                "account_id": DEFAULT_ACCOUNT_ID,
                "symbol": body.symbol,
                "side": body.side,
                "qty": body.qty,
                "order_type": body.order_type,
                "entry_price": body.entry_price,
                "stop_price": body.stop_price,
                "target_price": body.target_price,
                "status": "working",
                "created_ts": moment,
                "risk_usd": max_loss,
                "point_value": point_value,
                "setup_key": body.setup_key,
                "setup_id": body.setup_id,
                "note": body.note,
                "context": {
                    **body.context,
                    "equity_at_entry": equity,
                    "risk_budget_usd": size.risk_budget_usd,
                    "day_r_at_entry": brakes.day_r,
                    "week_r_at_entry": brakes.week_r,
                },
                "close_requested": False,
            }
        )
        publish_alert(
            {
                "kind": "paper",
                "event": "placed",
                "symbol": body.symbol,
                "message": f"Paper order #{order_id} {body.side.upper()} {body.qty}× {body.symbol} "
                f"{body.order_type} @ {body.entry_price:g}, stop {body.stop_price:g}"
                + (f", cíl {body.target_price:g}" if body.target_price is not None else "")
                + f" (riziko {max_loss:.0f} $)",
                "ts": moment.timestamp(),
            }
        )
        stored = repo.get_order(order_id)
        assert stored is not None
        return stored

    @router.delete("/orders/{order_id}")
    def close_or_cancel(order_id: int) -> dict[str, Any]:
        """Čekající order zruší, otevřenou pozici nechá zavřít enginem na dalším baru."""
        repo = repository_factory()
        order = repo.get_order(order_id)
        if order is None:
            raise HTTPException(404, "Order neexistuje")
        if order["status"] not in ("working", "open"):
            raise HTTPException(409, f"Order je ve stavu {order['status']}")
        repo.update_order(order_id, close_requested=True)
        updated = repo.get_order(order_id)
        assert updated is not None
        return updated

    @router.post("/kill")
    def kill(body: KillIn) -> dict[str, Any]:
        """Kill switch: účet zastaví, otevřené pozice zavře, čekající zruší."""
        repo = repository_factory()
        moment = now()
        repo.set_halted(True, body.reason, moment)
        for order in repo.active():
            repo.update_order(order.id, close_requested=True)
        publish_alert(
            {
                "kind": "paper",
                "event": "kill",
                "symbol": "*",
                "message": f"KILL SWITCH paper účtu: {body.reason} — pozice se zavírají, "
                "nové ordery blokovány",
                "ts": moment.timestamp(),
            }
        )
        return _account_payload(repo, params_factory(), moment)

    @router.post("/resume")
    def resume() -> dict[str, Any]:
        repo = repository_factory()
        repo.set_halted(False, None, now())
        return _account_payload(repo, params_factory(), now())

    @router.post("/events", status_code=201)
    def add_event(body: EventIn) -> dict[str, Any]:
        if body.kind not in EVENT_KINDS:
            raise HTTPException(422, f"kind musí být jeden z {EVENT_KINDS}")
        if body.kind in ("deposit", "withdraw") and body.amount <= 0:
            raise HTTPException(422, "amount musí být kladné")
        repo = repository_factory()
        event_id = repo.add_event(body.kind, body.amount, body.note, now())
        return {"id": event_id, "equity": repo.equity()}

    @router.get("/events")
    def events(limit: int = 100) -> dict[str, Any]:
        return {"events": repository_factory().events(limit=max(1, min(limit, 1000)))}

    return router
