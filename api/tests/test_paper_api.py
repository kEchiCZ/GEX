"""Paper účet API (#1187 fáze 1): účet, podání s blokací risk vrstvou, brzdy, kill switch."""

import datetime as dt
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from gexlens_api.paper_routes import build_paper_router
from gexlens_engine.compute.setups import SetupParams
from gexlens_engine.storage.paper_store import PaperRepository

NOW = dt.datetime(2026, 9, 17, 14, 0, tzinfo=dt.UTC)


def _app(tmp_path: Path, alerts: list[dict[str, Any]]) -> tuple[TestClient, PaperRepository]:
    repo = PaperRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}"))
    repo.ensure_schema()
    app = FastAPI()
    app.include_router(
        build_paper_router(lambda: repo, SetupParams, alerts.append, now=lambda: NOW)
    )
    return TestClient(app), repo


def _order(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "symbol": "ES",
        "side": "long",
        "qty": 1,
        "order_type": "limit",
        "entry_price": 7600.0,
        "stop_price": 7592.0,
        "target_price": 7616.0,
        "setup_key": "failed_break",
    }
    body.update(overrides)
    return body


def test_ucet_a_podani_orderu_v_rozpoctu(tmp_path: Path) -> None:
    alerts: list[dict[str, Any]] = []
    client, repo = _app(tmp_path, alerts)
    account = client.get("/paper/account").json()
    assert (
        account["equity"] == 50000.0
        and account["risk_budget_usd"] == 500.0
        and account["brake"] is None
    )
    # 8 b × 50 $ = 400 $ ≤ 500 $ → 201, working, riziko 400 $
    created = client.post("/paper/orders", json=_order())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "working" and body["risk_usd"] == 400.0 and body["point_value"] == 50.0
    assert body["context"]["equity_at_entry"] == 50000.0
    assert alerts and alerts[0]["kind"] == "paper" and alerts[0]["event"] == "placed"
    # Druhý order na týž symbol → 409 position_exists
    dup = client.post("/paper/orders", json=_order())
    assert dup.status_code == 409 and dup.json()["detail"]["block"] == "position_exists"
    # Zrušení čekajícího = close_requested (engine ho zruší na dalším baru)
    cancelled = client.delete(f"/paper/orders/{body['id']}")
    assert cancelled.status_code == 200 and cancelled.json()["close_requested"] is True
    assert client.get("/paper/account").json()["working"][0]["id"] == body["id"]


def test_risk_vrstva_blokuje(tmp_path: Path) -> None:
    alerts: list[dict[str, Any]] = []
    client, repo = _app(tmp_path, alerts)
    # 14 b × 50 $ = 700 $ > 500 $ → 409 stop_over_budget, max 0 ks
    over = client.post("/paper/orders", json=_order(stop_price=7586.0))
    assert over.status_code == 409
    detail = over.json()["detail"]
    assert detail["block"] == "stop_over_budget" and detail["max_contracts"] == 0
    # 2 ks × 8 b = 800 $ > 500 $ → 409 s max 1 ks
    two = client.post("/paper/orders", json=_order(qty=2))
    assert two.status_code == 409 and two.json()["detail"]["max_contracts"] == 1
    # Neplatné úrovně a neznámý symbol → 422
    assert client.post("/paper/orders", json=_order(stop_price=7605.0)).status_code == 422
    assert client.post("/paper/orders", json=_order(symbol="XYZ")).status_code == 422
    # Denní brzda: tři zavřené stopy dnes (−1 R každý) → 409 daily_brake
    for i in range(3):
        oid = repo.create_order(
            {
                "account_id": 1,
                "symbol": "NQ",
                "side": "short",
                "qty": 1,
                "order_type": "market",
                "entry_price": 29000.0,
                "stop_price": 29025.0,
                "target_price": None,
                "status": "closed",
                "created_ts": NOW - dt.timedelta(hours=2),
                "filled_ts": NOW - dt.timedelta(hours=2),
                "fill_price": 29000.0,
                "closed_ts": NOW - dt.timedelta(hours=1, minutes=i),
                "exit_price": 29025.0,
                "exit_reason": "stop",
                "pnl_usd": -510.0,
                "fees_usd": 10.0,
                "r_multiple": -1.0,
                "risk_usd": 500.0,
                "point_value": 20.0,
                "context": {},
                "close_requested": False,
            }
        )
        assert oid > 0
    account = client.get("/paper/account").json()
    assert (
        account["day_r"] == -3.0
        and account["brake"] == "daily_brake"
        and account["equity"] == 48470.0
    )
    blocked = client.post("/paper/orders", json=_order())
    assert blocked.status_code == 409 and blocked.json()["detail"]["block"] == "daily_brake"
    # Vklad zvedne equity i rozpočet
    deposit = client.post("/paper/events", json={"kind": "deposit", "amount": 5000})
    assert deposit.status_code == 201 and deposit.json()["equity"] == 53470.0
    assert client.post("/paper/events", json={"kind": "withdraw", "amount": 0}).status_code == 422


def test_kill_switch_zastavi_a_zavira(tmp_path: Path) -> None:
    alerts: list[dict[str, Any]] = []
    client, repo = _app(tmp_path, alerts)
    assert client.post("/paper/orders", json=_order()).status_code == 201
    killed = client.post("/paper/kill", json={"reason": "test"})
    assert killed.status_code == 200 and killed.json()["halted"] is True
    assert repo.active()[0].close_requested is True
    blocked = client.post(
        "/paper/orders",
        json=_order(symbol="NQ", entry_price=29000, stop_price=28980, target_price=29040),
    )
    assert blocked.status_code == 409 and blocked.json()["detail"]["block"] == "kill_switch"
    assert any(a["event"] == "kill" for a in alerts)
    resumed = client.post("/paper/resume")
    assert resumed.status_code == 200 and resumed.json()["halted"] is False


def test_posun_stopu_a_cile_s_historii(tmp_path: Path) -> None:
    alerts: list[dict[str, Any]] = []
    client, repo = _app(tmp_path, alerts)
    created = client.post("/paper/orders", json=_order()).json()
    oid = created["id"]
    # Přitažení stopu + nový cíl → OK, historie 2 změny
    moved = client.patch(
        f"/paper/orders/{oid}", json={"stop_price": 7595.0, "target_price": 7620.0}
    )
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert (
        body["stop_price"] == 7595.0
        and body["target_price"] == 7620.0
        and body["risk_usd"] == 250.0
    )
    assert [c["field"] for c in body["changes"]] == ["stop_price", "target_price"]
    assert any(a["event"] == "modified" for a in alerts)
    # Posun dál nad rozpočet (16 b × 50 = 800 > 500) → 409
    over = client.patch(f"/paper/orders/{oid}", json={"stop_price": 7584.0})
    assert over.status_code == 409 and over.json()["detail"]["block"] == "stop_over_budget"
    # Neplatná úroveň → 422; zrušení cíle → target None
    assert client.patch(f"/paper/orders/{oid}", json={"stop_price": 7605.0}).status_code == 422
    cleared = client.patch(f"/paper/orders/{oid}", json={"clear_target": True}).json()
    assert cleared["target_price"] is None
    assert client.patch("/paper/orders/999", json={"stop_price": 7590.0}).status_code == 404
