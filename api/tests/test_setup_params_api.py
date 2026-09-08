"""API parameter store setupů (#794 fáze 2, ADR-0033): čtení, nová verze, validace."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gexlens_api.main import create_app
from gexlens_engine.compute.setups import SetupParams, params_to_dict
from gexlens_engine.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Prázdná data a sqlite meta DB — store si schéma založí sám."""
    return Settings(
        data_dir=tmp_path,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}",
    )


def test_setup_params_read_and_append(settings: Settings) -> None:
    client = TestClient(create_app(settings))

    # Před prvním startem enginu: žádná verze, ale defaulty ke srovnání
    empty = client.get("/setups/params").json()
    assert empty["current"] is None and empty["history"] == []
    assert empty["defaults"] == params_to_dict(SetupParams())
    assert isinstance(empty["mechanics_version"], int)

    # Nová verze: jen změněné klíče, zbytek defaulty; created_by default „ui"
    created = client.post(
        "/setups/params",
        json={"params": {"wall_zone": 4.0, "acceptance_minutes": 6}, "note": "test širší zóny"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["version"] == 1 and body["created_by"] == "ui"
    assert body["params"]["wall_zone"] == 4.0 and body["params"]["acceptance_minutes"] == 6
    assert body["params"]["min_rrr"] == SetupParams().min_rrr

    current = client.get("/setups/params").json()
    assert current["current"]["version"] == 1
    assert [row["version"] for row in current["history"]] == [1]

    # Druhá verze — historie od nejnovější, první zůstává
    second = client.post(
        "/setups/params",
        json={"params": {}, "note": "návrat k defaultům", "created_by": "script"},
    )
    assert second.status_code == 201 and second.json()["version"] == 2
    assert [row["version"] for row in client.get("/setups/params").json()["history"]] == [2, 1]


def test_setup_params_validation(settings: Settings) -> None:
    client = TestClient(create_app(settings))
    # Neznámý klíč
    bad_key = client.post("/setups/params", json={"params": {"wall_zon": 3}, "note": "překlep"})
    assert bad_key.status_code == 422 and "Neznámé parametry" in bad_key.text
    # Špatný typ
    bad_type = client.post(
        "/setups/params", json={"params": {"acceptance_minutes": 2.5}, "note": "půl minuty"}
    )
    assert bad_type.status_code == 422
    # Bez důvodu se verze nezakládá (pydantic min_length)
    no_note = client.post("/setups/params", json={"params": {}, "note": ""})
    assert no_note.status_code == 422
    assert client.get("/setups/params").json()["current"] is None
