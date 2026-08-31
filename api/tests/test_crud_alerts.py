"""Integrační testy CRUD a alert enginu (issue #21)."""

import asyncio
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
        json={"symbol": "ES", "kind": "disconnect", "params": {"note": "provozni"}},
    )
    assert created.status_code == 201
    alert_id = created.json()["id"]
    assert created.json()["enabled"] is True

    invalid = client.post("/alerts", json={"symbol": "ES", "kind": "teleport"})
    assert invalid.status_code == 422  # neznámý druh alertu
    # Odstraněné druhy (#949 B) už API nesmí přijmout — pravidlo, které nikdo
    # nevyhodnocuje, je horší než odmítnutí
    zruseny = client.post("/alerts", json={"symbol": "ES", "kind": "price_cross"})
    assert zruseny.status_code == 422

    patched = client.patch(f"/alerts/{alert_id}", json={"enabled": False})
    assert patched.json()["enabled"] is False
    assert patched.json()["params"] == {"note": "provozni"}  # params nezměněny

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

    # Přesun tažením (#589): PUT přepíše payload a drží id, symbol i den
    moved = {"tool": "arrow", "color": "#ff0000", "points": [[3, 7620], [7, 7670]]}
    updated = client.put(f"/annotations/{annotation_id}", json={"payload": moved})
    assert updated.status_code == 200
    assert updated.json()["id"] == annotation_id
    assert updated.json()["payload"] == moved
    after = client.get("/annotations", params={"symbol": "ES", "date": "2026-07-16"}).json()
    assert after["annotations"] == [
        {"id": annotation_id, "symbol": "ES", "day": "2026-07-16", "payload": moved}
    ]
    assert client.put("/annotations/424242", json={"payload": moved}).status_code == 404

    assert client.delete(f"/annotations/{annotation_id}").status_code == 204
    assert client.delete(f"/annotations/{annotation_id}").status_code == 404


def test_settings_roundtrip(client: TestClient) -> None:
    assert client.get("/settings").json() == {"settings": {}}
    client.put("/settings/theme", json={"value": "dark"})
    client.put("/settings/hot_zone_width", json={"value": 15})
    client.put("/settings/theme", json={"value": "light"})  # upsert

    assert client.get("/settings").json() == {"settings": {"theme": "light", "hot_zone_width": 15}}


# ── Provozní alerty (#949 varianta B) ──────────────────────────────


def _drain(queue: asyncio.Queue[dict[str, object]]) -> list[dict[str, object]]:
    """Vybere všechny alerty z fronty subskribenta a vrátí jejich `data`."""
    messages = [queue.get_nowait() for _ in range(queue.qsize())]
    return [message["data"] for message in messages if isinstance(message["data"], dict)]


async def test_vypadek_spojeni_strili_jen_na_hrane() -> None:
    """Status chodí ~1×/min; bez hrany by zvoneček zvonil pořád dokola."""
    hub = LiveHub()
    subscriber_id, queue = hub.register()
    hub.subscribe(subscriber_id, ["alerts"])
    engine = AlertEngine(hub)

    assert engine.observe_connection("connected") is False
    assert engine.observe_connection("disconnected") is True  # hrana
    assert engine.observe_connection("disconnected") is False  # drží se, už nezvoní
    assert engine.observe_connection("connecting") is False
    assert engine.observe_connection("connected") is False  # návrat = natažení
    assert engine.observe_connection("disconnected") is True  # druhý výpadek zvoní znovu

    messages = _drain(queue)
    assert [m["kind"] for m in messages] == ["disconnect", "disconnect"]
    assert "disconnected" in str(messages[0]["message"])


async def test_chybejici_stav_spojeni_neni_vypadek() -> None:
    """Engine posílá do /internal/status jen změněné klíče — None nesmí zvonit."""
    engine = AlertEngine(LiveHub())
    assert engine.observe_connection(None) is False
    assert engine.observe_connection(None) is False


async def test_disk_limit_strili_jen_na_hrane() -> None:
    hub = LiveHub()
    subscriber_id, queue = hub.register()
    hub.subscribe(subscriber_id, ["alerts"])
    engine = AlertEngine(hub)

    assert engine.observe_disk(1_000_000_000, 5_000_000_000) is False
    assert engine.observe_disk(5_000_000_000, 5_000_000_000) is True  # dotyk limitu = překročení
    assert engine.observe_disk(6_000_000_000, 5_000_000_000) is False  # už zvonilo
    assert engine.observe_disk(2_000_000_000, 5_000_000_000) is False  # uklidilo se
    assert engine.observe_disk(9_000_000_000, 5_000_000_000) is True  # a znovu

    messages = _drain(queue)
    assert [m["kind"] for m in messages] == ["disk_limit", "disk_limit"]
    assert "GB" in str(messages[0]["message"])


async def test_disk_bez_pouzitelnych_cisel_nezvoni() -> None:
    engine = AlertEngine(LiveHub())
    assert engine.observe_disk(None, 5_000_000_000) is False
    assert engine.observe_disk(1_000, None) is False
    assert engine.observe_disk("hodne", "malo") is False
    assert engine.observe_disk(1_000, 0) is False  # limit nenastaven
