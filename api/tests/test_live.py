"""Testy /ws/live (issue #20): subscribe protokol, delta updaty, backpressure."""

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gexlens_api.live import LiveHub, SnapshotDeltaTracker, channel_matches
from gexlens_api.main import create_app
from gexlens_engine.config import Settings


def make_app(tmp_path: Path) -> FastAPI:
    app = create_app(Settings(data_dir=tmp_path))

    # Testovací publikační route: publish běží v event loopu aplikace (thread-safe)
    @app.post("/_test/publish")
    def _publish(channel: str, value: float) -> dict[str, int]:
        delivered: int = app.state.live_hub.publish(channel, {"value": value})
        return {"delivered": delivered}

    return app


# ── LiveHub jednotkově ─────────────────────────────────────────────


def test_channel_matching() -> None:
    assert channel_matches({"price.ES"}, "price.ES")
    assert not channel_matches({"price.ES"}, "price.SPY")
    assert channel_matches({"levels.*"}, "levels.ES.20260716")
    assert not channel_matches({"levels.*"}, "flow.ES")


async def test_backpressure_drops_oldest_frames() -> None:
    """AC: pomalý klient nepoloží server — publish nikdy neblokuje, staré framy padají."""
    hub = LiveHub(queue_size=5)
    subscriber_id, queue = hub.register()
    hub.subscribe(subscriber_id, ["status"])

    for i in range(20):  # klient nečte
        hub.publish("status", {"seq": i})

    assert queue.qsize() == 5
    received = [await queue.get() for _ in range(5)]
    data = received[0]["data"]
    assert isinstance(data, dict)
    assert [msg["data"]["seq"] for msg in received] == [15, 16, 17, 18, 19]  # type: ignore[index]


async def test_publish_returns_delivery_count_and_respects_channels() -> None:
    hub = LiveHub()
    id_a, queue_a = hub.register()
    id_b, _queue_b = hub.register()
    hub.subscribe(id_a, ["price.ES"])
    hub.subscribe(id_b, ["flow.ES"])

    assert hub.publish("price.ES", {"last": 7600.0}) == 1
    assert hub.publish("news", {"headline": "x"}) == 0
    message = await queue_a.get()
    assert message["channel"] == "price.ES"

    hub.unregister(id_a)
    assert hub.publish("price.ES", {"last": 7601.0}) == 0


# ── SnapshotDeltaTracker ───────────────────────────────────────────


def test_snapshot_delta_only_changed_cells() -> None:
    """AC/SPEC: snapshot kanál pushuje jen změněné buňky."""
    tracker = SnapshotDeltaTracker()
    minute_1 = [
        {"strike": 7600.0, "right": "C", "volume": 10.0, "oi": 100.0},
        {"strike": 7600.0, "right": "P", "volume": 5.0, "oi": 200.0},
    ]
    assert tracker.delta("ES", "20260716", minute_1) == minute_1  # první minuta = vše

    minute_2 = [
        {"strike": 7600.0, "right": "C", "volume": 12.0, "oi": 100.0},  # změna volume
        {"strike": 7600.0, "right": "P", "volume": 5.0, "oi": 200.0},  # beze změny
    ]
    changed = tracker.delta("ES", "20260716", minute_2)
    assert changed == [minute_2[0]]

    # Jiná expirace má vlastní stav
    other = tracker.delta("ES", "20260918", minute_1)
    assert other == minute_1


# ── WebSocket integračně ───────────────────────────────────────────


def test_ws_subscribe_receives_updates(tmp_path: Path) -> None:
    """AC: klient subscribe → přijímá delta updaty; nesubskribovaný kanál nechodí."""
    client = TestClient(make_app(tmp_path))
    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"action": "subscribe", "channels": ["price.ES", "snapshot.ES.20260716"]})
        ack = ws.receive_json()
        assert ack == {
            "type": "ack",
            "action": "subscribe",
            "channels": ["price.ES", "snapshot.ES.20260716"],
        }

        assert client.post("/_test/publish?channel=flow.ES&value=1").json()["delivered"] == 0
        assert client.post("/_test/publish?channel=price.ES&value=7600").json()["delivered"] == 1

        message = ws.receive_json()
        assert message["channel"] == "price.ES"
        assert message["data"] == {"value": 7600.0}


def test_ws_unsubscribe_and_unknown_action(tmp_path: Path) -> None:
    client = TestClient(make_app(tmp_path))
    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"action": "subscribe", "channels": ["status"]})
        assert ws.receive_json()["channels"] == ["status"]

        ws.send_json({"action": "unsubscribe", "channels": ["status"]})
        assert ws.receive_json()["channels"] == []

        assert client.post("/_test/publish?channel=status&value=1").json()["delivered"] == 0

        ws.send_json({"action": "restart"})
        assert ws.receive_json()["type"] == "error"


def test_ws_disconnect_unregisters(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = TestClient(app)
    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"action": "subscribe", "channels": ["status"]})
        ws.receive_json()
    # Po odpojení klient zmizel z hubu — publish nemá komu doručit
    assert client.post("/_test/publish?channel=status&value=1").json()["delivered"] == 0


async def test_slow_client_does_not_block_publish() -> None:
    hub = LiveHub(queue_size=2)
    subscriber_id, _queue = hub.register()
    hub.subscribe(subscriber_id, ["status"])

    async with asyncio.timeout(1.0):  # publish 10k zpráv nesmí blokovat ani spadnout
        for i in range(10_000):
            hub.publish("status", {"seq": i})
