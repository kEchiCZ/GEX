"""Verdikt dne z Briefingu (#1090): upsert per seance × symbol, výpis, validace."""

import datetime as dt
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gexlens_api.main import create_app
from gexlens_engine.config import Settings

TODAY = dt.datetime.now(dt.UTC).date()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}",
    )
    return TestClient(create_app(settings))


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "session_date": TODAY.isoformat(),
        "symbol": "ES",
        "verdict": "long",
        "score": 4,
        "votes": [
            {"name": "trend_higher", "vote": 2, "reason": "týden i den rostoucí"},
            {"name": "gamma", "vote": 1, "reason": "negativní gamma = momentum"},
        ],
        "rules_version": 1,
    }
    base.update(overrides)
    return base


def test_verdict_upsert_je_idempotentni(client: TestClient) -> None:
    first = client.post("/briefing/verdicts", json=_payload())
    assert first.status_code == 201
    created = first.json()
    assert created["verdict"] == "long"
    assert created["votes"][0]["name"] == "trend_higher"

    # Před openem se verdikt může změnit — stejná seance a symbol přepíše, nezdvojí
    second = client.post("/briefing/verdicts", json=_payload(verdict="none", score=1))
    assert second.status_code == 201
    assert second.json()["id"] == created["id"]
    assert second.json()["verdict"] == "none"

    listed = client.get("/briefing/verdicts", params={"symbol": "ES"}).json()["verdicts"]
    assert len(listed) == 1
    assert listed[0]["score"] == 1
    assert listed[0]["session_date"] == TODAY.isoformat()


def test_verdicts_per_symbol_a_okno_dnu(client: TestClient) -> None:
    client.post("/briefing/verdicts", json=_payload())
    client.post("/briefing/verdicts", json=_payload(symbol="NQ", verdict="short", score=-3))
    old_day = (TODAY - dt.timedelta(days=60)).isoformat()
    client.post("/briefing/verdicts", json=_payload(session_date=old_day, verdict="wait_news"))

    all_recent = client.get("/briefing/verdicts", params={"days": 30}).json()["verdicts"]
    assert {row["symbol"] for row in all_recent} == {"ES", "NQ"}
    nq = client.get("/briefing/verdicts", params={"symbol": "NQ"}).json()["verdicts"]
    assert [row["verdict"] for row in nq] == ["short"]
    older = client.get("/briefing/verdicts", params={"days": 90}).json()["verdicts"]
    assert len(older) == 3


def test_verdict_validace(client: TestClient) -> None:
    assert client.post("/briefing/verdicts", json=_payload(verdict="maybe")).status_code == 422
    assert client.post("/briefing/verdicts", json=_payload(score=99)).status_code == 422
