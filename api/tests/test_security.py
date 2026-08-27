"""Testy bezpečnostních kontrol API (#542).

Kryjí čtyři nálezy prověrky: token na interním ingestu a záloze (C3/C5),
whitelist zapisovatelných nastavení (C4), Origin u WebSocketu (H1) a stropy
subskripcí (H2).
"""

from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from gexlens_api.crud import ENGINE_SETTINGS, WRITABLE_SETTINGS
from gexlens_api.data import DataRepository, OutsideDataDirError
from gexlens_api.live import (
    MAX_CHANNELS_PER_SUBSCRIBER,
    LiveHub,
    TooManyChannels,
    TooManySubscribers,
    parse_channels,
)
from gexlens_api.main import create_app
from gexlens_api.security import origin_allowed
from gexlens_engine.config import Settings
from gexlens_engine.ibkr.account import classify_accounts
from gexlens_engine.runtime_settings import CONNECTION_SETTINGS, RUNTIME_SETTINGS

TOKEN = "sdilene-tajemstvi"


@pytest.fixture
def app_settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path)


@pytest.fixture
def client(app_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("GEXLENS_API_TOKEN", TOKEN)
    return TestClient(create_app(app_settings))


@pytest.fixture
def tokenless_client(app_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("GEXLENS_API_TOKEN", raising=False)
    return TestClient(create_app(app_settings))


# ── Token na interním ingestu a záloze (C3, C5) ────────────────────────────


def test_internal_publish_bez_tokenu_odmitnuto(client: TestClient) -> None:
    """Podvržení `price.*` do grafu bylo možné bez jakéhokoli ověření."""
    response = client.post("/internal/publish", json={"channel": "price.ES", "data": {"last": 1.0}})
    assert response.status_code == 401


def test_internal_publish_se_spatnym_tokenem_odmitnuto(client: TestClient) -> None:
    response = client.post(
        "/internal/publish",
        json={"channel": "price.ES", "data": {"last": 1.0}},
        headers={"X-GEXLens-Token": TOKEN + "x"},
    )
    assert response.status_code == 401


def test_internal_status_s_tokenem_projde(client: TestClient) -> None:
    response = client.post(
        "/internal/status", json={"engine": "online"}, headers={"X-GEXLens-Token": TOKEN}
    )
    assert response.status_code == 200
    assert client.get("/status").json()["engine"] == "online"


def test_zaloha_bez_tokenu_odmitnuta(client: TestClient) -> None:
    """Dump nese celý nenahraditelný archiv — nesmí jít stáhnout anonymně."""
    assert client.get("/backup/postgres").status_code == 401


def test_bez_nastaveneho_tokenu_je_endpoint_vypnuty(tokenless_client: TestClient) -> None:
    """Prázdný token neznamená „otevřeno" — to by se tiše přeneslo do provozu."""
    response = tokenless_client.post("/internal/status", json={"engine": "online"})
    assert response.status_code == 503
    assert "GEXLENS_API_TOKEN" in response.json()["detail"]


def test_cteci_endpointy_token_nevyzaduji(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    assert client.get("/status").status_code == 200


# ── Whitelist nastavení (C4) ───────────────────────────────────────────────


def test_neznamy_klic_nastaveni_odmitnut(client: TestClient) -> None:
    """`retention_days=1` nebo cizí `ibkr_host` byly neautentizované řízení enginu."""
    response = client.put("/settings/muj_klic", json={"value": 1})
    assert response.status_code == 422
    assert "Neznámý klíč" in response.json()["detail"]


def test_whitelist_pokryva_vse_co_engine_cte() -> None:
    """Whitelist se odvozuje z RUNTIME/CONNECTION_SETTINGS, ať nezaostane za enginem."""
    expected = (
        {spec.key for spec in RUNTIME_SETTINGS}
        | {spec.key for spec in CONNECTION_SETTINGS}
        | {"ibkr_host", "subscription_alert_enabled"}
    )
    assert expected == ENGINE_SETTINGS
    assert "theme" in WRITABLE_SETTINGS  # předvolba UI, engine ji nečte


def test_seznamy_zdroju_validuji_tvar(client: TestClient) -> None:
    """#578: news_* klíče berou jen seznam řetězců — jiný tvar by news-engine
    tiše ignoroval jako 'uživatel vše smazal'. (Validace běží před zápisem,
    takže fixture bez DB stačí; úspěšný zápis testuje test_sentiment_api.)"""
    for bad in [{"value": "cnbc.com"}, {"value": [1]}, {"value": [""]}, {"value": ["x" * 301]}]:
        response = client.put("/settings/news_bluesky_authors", json=bad)
        assert response.status_code == 422, bad
    assert "retro_pass" not in WRITABLE_SETTINGS  # píše si ho news-engine přímo do DB


# ── Origin u WebSocketu (H1) ───────────────────────────────────────────────


def test_ws_odmita_cizi_origin(client: TestClient) -> None:
    """CORS se na WS handshake nevztahuje — bez téhle kontroly četla data cizí stránka."""
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws/live", headers={"Origin": "https://evil.example"}),
    ):
        pass  # spojení server zavře dřív, než se dá poslat subscribe


def test_ws_pusti_vlastni_frontend(client: TestClient) -> None:
    with client.websocket_connect("/ws/live", headers={"Origin": "http://127.0.0.1:8080"}) as ws:
        ws.send_json({"action": "subscribe", "channels": ["status"]})
        assert ws.receive_json()["type"] == "ack"


def test_origin_allowed_rozhodovani() -> None:
    assert origin_allowed(None, "api:8000", []) is True  # engine, curl, testy
    assert origin_allowed("http://localhost:5173", "127.0.0.1:8000", []) is True
    assert origin_allowed("https://evil.example", "gexlens:8080", []) is False
    # Same-origin za nginx: Host stránky se shoduje s originem
    assert origin_allowed("http://gexlens:8080", "gexlens:8080", []) is True
    assert origin_allowed("https://ui.ts.net", "gexlens:8080", ["https://ui.ts.net"]) is True


# ── Stropy WS (H2) ─────────────────────────────────────────────────────────


def test_retezec_kanalu_se_neiteruje_po_znacich() -> None:
    """`{"channels": "abc"}` dřív subskriboval kanály `a`, `b`, `c`."""
    with pytest.raises(ValueError, match="seznam"):
        parse_channels("abc")


def test_neplatny_typ_kanalu_neshodi_spojeni() -> None:
    with pytest.raises(ValueError):
        parse_channels([5])
    with pytest.raises(ValueError, match="Neplatný název"):
        parse_channels(["price.ES; rm -rf"])


def test_ws_neplatny_kanal_vrati_chybu_a_spojeni_zije(client: TestClient) -> None:
    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"action": "subscribe", "channels": "abc"})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"action": "subscribe", "channels": ["status"]})
        assert ws.receive_json()["type"] == "ack"


def test_strop_kanalu_na_spojeni() -> None:
    hub = LiveHub()
    subscriber_id, _ = hub.register()
    hub.subscribe(subscriber_id, [f"ch{i}" for i in range(MAX_CHANNELS_PER_SUBSCRIBER)])
    with pytest.raises(TooManyChannels):
        hub.subscribe(subscriber_id, ["jeden-navic"])


def test_strop_souběžných_spojeni() -> None:
    hub = LiveHub(max_subscribers=2)
    hub.register()
    hub.register()
    with pytest.raises(TooManySubscribers):
        hub.register()


# ── Traversal a maskování účtu (M6, M7) ────────────────────────────────────


def test_cesta_mimo_datovy_adresar_je_odmitnuta(app_settings: Settings) -> None:
    """`..` v `symbol`/`expiry` se dřív dostalo až do konstrukce cesty."""
    repo = DataRepository(app_settings)
    with pytest.raises(OutsideDataDirError):
        repo.levels("..", "..", __import__("datetime").date(2026, 1, 1))


def test_404_neprozrazuje_cestu_na_disku(client: TestClient) -> None:
    response = client.get("/levels/ES/20260807", params={"date": "2026-01-01"})
    assert response.status_code == 404
    assert "/" not in response.json()["detail"]


def test_cislo_uctu_se_maskuje() -> None:
    """Štítek jde do neautentizovaného /status — koncovka na rozlišení stačí."""
    assert classify_accounts(["U1234567"]).label == "U***567 (živý)"
    assert classify_accounts(["DUR628329"]).label == "DU***329 (paper)"
