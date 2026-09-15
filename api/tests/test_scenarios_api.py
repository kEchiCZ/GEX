"""Scénář dne (#1173): založení jen dopředu, snímek na disk, seznam, statistiky, úklid."""

import base64
import datetime as dt
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from gexlens_api import scenario_routes
from gexlens_api.scenario_routes import build_scenario_router
from gexlens_engine.storage.scenarios_store import ScenariosRepository

NOW = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _app(tmp_path: Path, alerts: list[dict[str, Any]]) -> tuple[TestClient, ScenariosRepository]:
    repo = ScenariosRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}"))
    repo.ensure_schema()
    app = FastAPI()
    app.include_router(
        build_scenario_router(lambda: repo, tmp_path, alerts.append, now=lambda: NOW)
    )
    return TestClient(app), repo


def _payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "symbol": "ES",
        "entry": 7600.0,
        "path": [
            {"ts": NOW.isoformat(), "price": 7600},
            {"ts": (NOW + dt.timedelta(hours=1)).isoformat(), "price": 7680},
            {"ts": (NOW + dt.timedelta(hours=2)).isoformat(), "price": 7600},
        ],
        "image_png_base64": base64.b64encode(PNG).decode(),
    }
    body.update(overrides)
    return body


def test_zalozeni_odvodi_cile_ulozi_snimek_a_vrati_obrazek(tmp_path: Path) -> None:
    alerts: list[dict[str, Any]] = []
    client, repo = _app(tmp_path, alerts)
    created = client.post("/scenarios", json=_payload())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["day"] == "2026-09-15" and body["deadline"] == "2026-09-15"
    assert body["deadline_ts"].startswith("2026-09-15T20:00")  # settle dne v létě
    assert body["targets"] == [7680.0, 7600.0] and body["has_image"] is True
    assert body["image_bytes"] == len(PNG) and body["result"] is None
    stored = tmp_path / "scenarios" / "ES" / "2026-09-15" / f"{body['id']}.png"
    assert stored.read_bytes() == PNG
    image = client.get(f"/scenarios/{body['id']}/image")
    assert image.status_code == 200 and image.content == PNG
    listed = client.get("/scenarios?symbol=ES").json()["scenarios"]
    assert [row["id"] for row in listed] == [body["id"]]
    assert alerts == []  # pod limitem žádný alert
    assert client.get("/scenarios/disk").json()["bytes"] == len(PNG)


def test_termin_v_minulosti_a_nesmyslny_snimek(tmp_path: Path) -> None:
    client, _ = _app(tmp_path, [])
    assert client.post("/scenarios", json=_payload(deadline="2026-09-14")).status_code == 422
    assert client.post("/scenarios", json=_payload(deadline="2026-12-31")).status_code == 422
    bad = client.post(
        "/scenarios", json=_payload(image_png_base64=base64.b64encode(b"GIF89a").decode())
    )
    assert bad.status_code == 422
    # Bez snímku jde založit (jen řádek), budoucí termín platí
    ok = client.post("/scenarios", json=_payload(image_png_base64=None, deadline="2026-09-18"))
    assert ok.status_code == 201 and ok.json()["has_image"] is False
    assert ok.json()["deadline"] == "2026-09-18"


def test_alert_nad_limitem_a_uklid_snimku(tmp_path: Path, monkeypatch: Any) -> None:
    alerts: list[dict[str, Any]] = []
    monkeypatch.setattr(scenario_routes, "SCENARIO_DISK_LIMIT", 100)
    client, repo = _app(tmp_path, alerts)
    first = client.post("/scenarios", json=_payload()).json()
    second = client.post("/scenarios", json=_payload()).json()
    # 72 B + 72 B ≥ 100 B → jeden alert (hranově), druhé překročení už nezvoní
    assert len(alerts) == 1 and alerts[0]["kind"] == "scenario_disk"
    assert client.get("/scenarios/disk").json()["over_limit"] is True
    # Úklid starších než 1 den nic (oba dnes); starší než 0 dní nejde (ge=1)
    assert client.delete("/scenarios/images?older_than_days=1").json()["removed"] == 0
    repo.set_image(first["id"], f"scenarios/ES/2026-09-15/{first['id']}.png", len(PNG))
    # Posuneme vznik prvního do minulosti a uklidíme
    from sqlalchemy import update

    from gexlens_engine.storage.scenarios_store import scenarios_table

    with repo._engine.begin() as conn:  # noqa: SLF001 — test sahá na fixture přímo
        conn.execute(
            update(scenarios_table)
            .where(scenarios_table.c.id == first["id"])
            .values(created_at=NOW - dt.timedelta(days=10))
        )
    cleaned = client.delete("/scenarios/images?older_than_days=5").json()
    assert cleaned["removed"] == 1 and cleaned["images"] == 1
    assert not (tmp_path / "scenarios" / "ES" / "2026-09-15" / f"{first['id']}.png").exists()
    assert client.get(f"/scenarios/{first['id']}/image").status_code == 404
    assert client.get(f"/scenarios/{second['id']}/image").status_code == 200
    # Řádek s (budoucím) výsledkem zůstal
    assert client.get("/scenarios").json()["scenarios"][1]["id"] == first["id"]
    # Smazání scénáře odstraní i soubor
    assert client.delete(f"/scenarios/{second['id']}").status_code == 204
    assert client.get("/scenarios/disk").json()["images"] == 0
    stats = client.get("/scenarios/stats?symbol=ES").json()
    assert stats["n"] == 0 and stats["preliminary"] is True
