"""API stádií buněk (#1323): POST /setups/stage, ochrana stádií v POST /setups/params
a buňky Knihovny v GET /setups/summary."""

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from gexlens_api.main import create_app
from gexlens_engine.compute.setups import SETUP_MECHANICS_VERSION, SetupParams
from gexlens_engine.config import Settings
from gexlens_engine.storage.setup_params_store import SetupParamsRepository
from gexlens_engine.storage.setups_store import SetupsRepository

CELL = "NQ:trend_continuation"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}",
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    """API se seedem parametrů — jako po prvním startu enginu."""
    repo = SetupParamsRepository(create_engine(settings.database_url))
    repo.ensure_schema()
    repo.save(SetupParams(), note="seed", created_by="engine")
    return TestClient(create_app(settings))


def _stage(client: TestClient, **body: Any) -> Any:
    return client.post("/setups/stage", json={"cell": CELL, "note": "test naživo", **body})


def test_stadium_nova_verze_a_zacatek_zkousky_nastavi_server(client: TestClient) -> None:
    shadow = _stage(client, stage="shadow", note="edge neprokázán")
    assert shadow.status_code == 201, shadow.text
    body = shadow.json()
    assert body["version"] == 2 and body["note"] == "edge neprokázán"
    assert body["params"]["shadow_cells"] == [CELL] and body["params"]["trial_cells"] == {}

    before = dt.datetime.now(dt.UTC)
    trial = _stage(client, stage="trial")
    after = dt.datetime.now(dt.UTC)
    assert trial.status_code == 201, trial.text
    params = trial.json()["params"]
    assert params["shadow_cells"] == []  # Stín → Zkouška přímo
    started = dt.datetime.fromisoformat(params["trial_cells"][CELL]["started_at"])
    assert before <= started <= after
    # Bez rozpočtu = výchozí z parametrů (10 setupů / −3 R)
    assert params["trial_cells"][CELL]["budget_setups"] == 10
    assert params["trial_cells"][CELL]["budget_r"] == 3.0
    # Mechaniku zkoušky razí server — po jejím zvednutí zkouška skončí
    assert params["trial_cells"][CELL]["mechanics_version"] == SETUP_MECHANICS_VERSION

    # Obnovení zkoušky = nový začátek a nový rozpočet (počítá od nuly)
    renewed = _stage(client, stage="trial", budget_setups=5, budget_r=1.5, note="obnova")
    assert renewed.status_code == 201
    again = renewed.json()["params"]["trial_cells"][CELL]
    assert dt.datetime.fromisoformat(again["started_at"]) >= started
    assert (again["budget_setups"], again["budget_r"]) == (5, 1.5)

    back = _stage(client, stage="auto", note="konec testu")
    assert back.status_code == 201 and back.json()["params"]["trial_cells"] == {}
    history = client.get("/setups/params").json()["history"]
    assert [row["version"] for row in history] == [5, 4, 3, 2, 1]
    # Auto → Auto nic nemění: 409, žádná prázdná verze
    same = _stage(client, stage="auto")
    assert same.status_code == 409
    assert client.get("/setups/params").json()["current"]["version"] == 5


@pytest.mark.parametrize(
    ("body", "status", "text"),
    [
        ({"cell": "NQ-trend_continuation", "stage": "shadow"}, 422, "formát buňky"),
        ({"cell": "nq:trend_continuation", "stage": "shadow"}, 422, "ticker piš jako"),
        ({"cell": "NQ:fib_618", "stage": "shadow"}, 422, "Neznámá šablona"),
        ({"stage": "trial", "budget_setups": 21}, 422, "mimo meze 1–20"),
        ({"stage": "trial", "budget_setups": 0}, 422, "mimo meze 1–20"),
        ({"stage": "trial", "budget_r": 0.4}, 422, "mimo meze 0.5–6 R"),
        ({"stage": "trial", "budget_r": 6.5}, 422, "mimo meze 0.5–6 R"),
        ({"stage": "trial", "budget_setups": 10.5}, 422, "budget_setups"),
        ({"stage": "shadow", "budget_setups": 5}, 422, "jen ke zkoušce"),
        ({"stage": "shadow", "note": "   "}, 422, "note"),
        ({"stage": "shadow", "note": ""}, 422, "note"),
        ({"stage": "trade"}, 422, "stage"),
    ],
)
def test_stadium_validace(client: TestClient, body: dict[str, Any], status: int, text: str) -> None:
    response = client.post("/setups/stage", json={"cell": CELL, "note": "důvod", **body})
    assert response.status_code == status, response.text
    assert text in response.text
    assert client.get("/setups/params").json()["current"]["version"] == 1  # nic se nezapsalo


def test_stadium_bez_verze_parametru_409(settings: Settings) -> None:
    client = TestClient(create_app(settings))
    response = client.post(
        "/setups/stage", json={"cell": CELL, "stage": "shadow", "note": "před seedem"}
    )
    assert response.status_code == 409 and "engine ji založí" in response.text


def test_params_nemeni_stadia(client: TestClient) -> None:
    assert _stage(client, stage="trial").status_code == 201
    current = client.get("/setups/params").json()["current"]["params"]
    # Celá platná sada (vzor Settings → Risk): stádia beze změny projdou
    same = client.post(
        "/setups/params", json={"params": {**current, "risk_pct": 0.5}, "note": "menší riziko"}
    )
    assert same.status_code == 201, same.text
    assert same.json()["params"]["trial_cells"] == current["trial_cells"]
    # Bez klíčů stádií: převezmou se z platné verze (ne „defaulty = vše Auto“)
    omitted = client.post("/setups/params", json={"params": {"risk_pct": 1.0}, "note": "zpět"})
    assert omitted.status_code == 201
    assert omitted.json()["params"]["trial_cells"] == current["trial_cells"]
    # Jiná stádia (starý snímek, vlastní začátek zkoušky) = 422
    forged = {
        CELL: {**current["trial_cells"][CELL], "started_at": "2030-01-01T00:00:00+00:00"},
    }
    changes: list[dict[str, Any]] = [
        {"trial_cells": forged},
        {"trial_cells": {}},
        {"shadow_cells": ["ES:trend_continuation"]},
    ]
    for changed in changes:
        response = client.post(
            "/setups/params", json={"params": {**current, **changed}, "note": "pokus"}
        )
        assert response.status_code == 422, response.text
        assert "POST /setups/stage" in response.text
    # Prázdný důvod z mezer = 422, ne 500
    blank = client.post("/setups/params", json={"params": {}, "note": "    "})
    assert blank.status_code == 422


def test_summary_vraci_bunky_knihovny(client: TestClient, settings: Settings) -> None:
    repo = SetupsRepository(create_engine(settings.database_url))
    repo.ensure_schema()
    now = dt.datetime.now(dt.UTC)
    for day in range(1, 4):
        created = now - dt.timedelta(days=day)
        sid = repo.create(
            symbol="NQ",
            expiry=(created + dt.timedelta(days=1)).strftime("%Y%m%d"),
            template="trend_continuation",
            direction="long",
            created_ts=created,
            entry=29000.0,
            target=29040.0,
            stop=28984.0,
            confidence=50,
            reason="test",
            context={"affordable": True, "tradeable": False, "trade_block": "user", "contracts": 1},
        )
        repo.close(
            sid,
            status="closed_target",
            closed_ts=created + dt.timedelta(minutes=20),
            outcome_r=2.5,
            mfe=40.0,
            mae=1.0,
        )
    assert _stage(client, stage="shadow").status_code == 201
    summary = client.get("/setups/summary?symbols=NQ").json()
    assert summary["mechanics_version"] == SETUP_MECHANICS_VERSION
    assert summary["shadow_reasons"] == {"user": 3}
    assert summary["trial"]["count"] == 0
    cells = {cell["cell"]: cell for cell in summary["cells"]}
    assert set(cells) == {
        "NQ:wall_bounce",
        "NQ:failed_break",
        "NQ:max_pain_pin",
        "NQ:gamma_momentum",
        "NQ:trend_continuation",
    }
    t7 = cells[CELL]
    assert (t7["stage"], t7["effective_stage"], t7["trial"]) == ("shadow", "shadow", None)
    assert (t7["gate_verdict"], t7["gate_n"]) == ("insufficient", 3)
    assert t7["avg_r"] == 2.5 and t7["avg_net_r"] == pytest.approx(2.5 - 1.74 / 32)
    assert t7["template_number"] == 7 and t7["ticker"] == "NQ"
    # Σ čistě v mikro dolarech, kontrakty z kontextu: 3 × 1 × (2,5 R × 16 b × 2 $ − 1,74 $)
    assert t7["net_usd"] == pytest.approx(3 * (2.5 * 16 * 2.0 - 1.74))
    assert isinstance(t7["window_capacity"], int)
    # Brzdy účtu (hlavička Knihovny): stín se nepočítá, limity z platné verze
    brakes = summary["brakes"]
    assert (brakes["day_r"], brakes["week_r"], brakes["block"]) == (0.0, 0.0, None)
    assert (brakes["daily_brake_r"], brakes["weekly_brake_r"]) == (3.0, 6.0)
    assert brakes["template_stops"] == {}
    # Ticker bez setupů v okně a bez stádia buňky nemá; buňky s nastaveným
    # stádiem jsou vidět vždy, i mimo `symbols` (pinovaný kontrakt mimo
    # watchlist), souhrn sám zůstává jen pro `symbols`; brzdy napříč symboly
    es = client.get("/setups/summary?symbols=ES").json()
    assert {cell["ticker"] for cell in es["cells"]} == {"NQ"}
    assert next(cell for cell in es["cells"] if cell["cell"] == CELL)["gate_n"] == 3
    assert es["all"]["count"] == 0 and es["shadow_reasons"] == {}
    assert es["brakes"] == brakes
