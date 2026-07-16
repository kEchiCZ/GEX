"""Integrační testy CRUD a alert enginu (issue #21)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gexlens_api.alerts import AlertEngine
from gexlens_api.live import LiveHub
from gexlens_api.main import create_app
from gexlens_engine.config import Settings


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.db'}",
    )
    return TestClient(create_app(settings))


# ── CRUD integračně (AC) ───────────────────────────────────────────


def test_watchlist_crud(client: TestClient) -> None:
    assert client.get("/watchlist").json() == {"watchlist": []}

    created = client.post("/watchlist", json={"symbol": "ES"})
    assert created.status_code == 201
    item_id = created.json()["id"]
    client.post("/watchlist", json={"symbol": "SPY"})

    listed = client.get("/watchlist").json()["watchlist"]
    assert [item["symbol"] for item in listed] == ["ES", "SPY"]

    assert client.post("/watchlist", json={"symbol": "ES"}).status_code == 409  # duplicita
    assert client.delete(f"/watchlist/{item_id}").status_code == 204
    assert client.delete(f"/watchlist/{item_id}").status_code == 404
    assert [i["symbol"] for i in client.get("/watchlist").json()["watchlist"]] == ["SPY"]


def test_alerts_crud_and_validation(client: TestClient) -> None:
    created = client.post(
        "/alerts",
        json={"symbol": "ES", "kind": "price_cross", "params": {"level_source": "flip"}},
    )
    assert created.status_code == 201
    alert_id = created.json()["id"]
    assert created.json()["enabled"] is True

    invalid = client.post("/alerts", json={"symbol": "ES", "kind": "teleport"})
    assert invalid.status_code == 422  # neznámý druh alertu

    patched = client.patch(f"/alerts/{alert_id}", json={"enabled": False})
    assert patched.json()["enabled"] is False
    assert patched.json()["params"] == {"level_source": "flip"}  # params nezměněny

    assert client.patch(f"/alerts/{alert_id}", json={}).status_code == 422
    assert client.delete(f"/alerts/{alert_id}").status_code == 204
    assert client.get("/alerts").json() == {"alerts": []}


def test_annotations_crud(client: TestClient) -> None:
    payload = {"tool": "arrow", "color": "#ff0000", "points": [[1, 7600], [5, 7650]]}
    created = client.post(
        "/annotations", json={"symbol": "ES", "day": "2026-07-16", "payload": payload}
    )
    assert created.status_code == 201

    listed = client.get("/annotations", params={"symbol": "ES", "date": "2026-07-16"}).json()
    assert len(listed["annotations"]) == 1
    assert listed["annotations"][0]["payload"] == payload

    other_day = client.get("/annotations", params={"symbol": "ES", "date": "2026-07-15"}).json()
    assert other_day["annotations"] == []  # persistence per instrument+den (SPEC 7.4)

    annotation_id = created.json()["id"]
    assert client.delete(f"/annotations/{annotation_id}").status_code == 204
    assert client.delete(f"/annotations/{annotation_id}").status_code == 404


def test_settings_roundtrip(client: TestClient) -> None:
    assert client.get("/settings").json() == {"settings": {}}
    client.put("/settings/theme", json={"value": "dark"})
    client.put("/settings/hot_zone_width", json={"value": 15})
    client.put("/settings/theme", json={"value": "light"})  # upsert

    assert client.get("/settings").json() == {"settings": {"theme": "light", "hot_zone_width": 15}}


# ── Alert engine (AC: cross nad syntetickou cenovou řadou) ─────────


async def test_price_cross_over_synthetic_series() -> None:
    hub = LiveHub()
    subscriber_id, queue = hub.register()
    hub.subscribe(subscriber_id, ["alerts"])
    engine = AlertEngine(hub)
    flip = 7600.0

    series = [7590.0, 7595.0, 7602.0, 7598.0, 7605.0]
    fired = [engine.price_cross(1, "ES", price, flip, "flip") for price in series]

    # První vzorek nemá historii; crossy: 7595→7602 (nahoru), 7602→7598 (dolů), 7598→7605
    assert fired == [False, False, True, True, True]
    messages = [queue.get_nowait() for _ in range(queue.qsize())]
    assert len(messages) == 3
    first = messages[0]["data"]
    assert isinstance(first, dict)
    assert first["kind"] == "price_cross"
    assert "flip" in str(first["message"])


async def test_price_touch_without_cross_does_not_fire() -> None:
    engine = AlertEngine(LiveHub())
    assert engine.price_cross(1, "ES", 7590.0, 7600.0, "flip") is False
    assert engine.price_cross(1, "ES", 7600.0, 7600.0, "flip") is True  # dotyk = cross na úroveň
    assert engine.price_cross(1, "ES", 7600.0, 7600.0, "flip") is False  # stojí na úrovni


async def test_cum_delta_jump_threshold() -> None:
    hub = LiveHub()
    subscriber_id, queue = hub.register()
    hub.subscribe(subscriber_id, ["alerts"])
    engine = AlertEngine(hub)

    fired = [
        engine.cum_delta_jump(2, "ES", value, threshold=400.0)
        for value in [0.0, 300.0, 350.0, 800.0]
    ]
    assert fired == [False, False, False, True]  # skoky 300, 50, 450
    assert queue.qsize() == 1


async def test_dominant_strike_change_and_ops_alerts() -> None:
    hub = LiveHub()
    subscriber_id, queue = hub.register()
    hub.subscribe(subscriber_id, ["alerts"])
    engine = AlertEngine(hub)

    assert engine.dominant_strike_change(3, "ES", 7600.0) is False
    assert engine.dominant_strike_change(3, "ES", 7600.0) is False
    assert engine.dominant_strike_change(3, "ES", 7650.0) is True

    engine.connection_lost("TWS socket uzavřen")
    engine.disk_limit_exceeded(2_000_000_000, 1_000_000_000)
    kinds = [queue.get_nowait()["data"]["kind"] for _ in range(queue.qsize())]  # type: ignore[index]
    assert kinds == ["dominant_strike_change", "disconnect", "disk_limit"]
