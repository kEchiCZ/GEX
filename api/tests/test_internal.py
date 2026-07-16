"""Testy interního ingestu (issue #30): engine → API → status + WS kanály."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gexlens_api.main import create_app
from gexlens_engine.config import Settings


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(Settings(data_dir=tmp_path)))


def test_internal_status_updates_and_broadcasts(client: TestClient) -> None:
    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"action": "subscribe", "channels": ["status"]})
        ws.receive_json()  # ack

        response = client.post(
            "/internal/status",
            json={"engine": "online", "greeks_complete": 300, "greeks_total": 360},
        )
        assert response.status_code == 200

        message = ws.receive_json()
        assert message["channel"] == "status"
        assert message["data"]["engine"] == "online"

    payload = client.get("/status").json()
    assert payload["engine"] == "online"
    assert payload["greeks_complete"] == 300


def test_internal_publish_routes_to_channel(client: TestClient) -> None:
    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"action": "subscribe", "channels": ["price.ES"]})
        ws.receive_json()

        response = client.post(
            "/internal/publish", json={"channel": "price.ES", "data": {"last": 7601.5}}
        )
        assert response.json() == {"delivered": 1}
        assert ws.receive_json()["data"] == {"last": 7601.5}

    invalid = client.post("/internal/publish", json={"channel": 42})
    assert invalid.status_code == 422
