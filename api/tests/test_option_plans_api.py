"""Plán opce QQQ v API (F4a #1743, ADR-0062): parametry účtu v `/settings`.

Čísla účtu jsou smyšlená (velikost skutečného účtu do repa nepatří).
"""

import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gexlens_api.crud import WRITABLE_SETTINGS
from gexlens_api.main import create_app
from gexlens_engine.compute.option_plan import ACCOUNT_KEY, ACCOUNT_KEYS, MAX_LOSS_KEY
from gexlens_engine.config import Settings
from gexlens_engine.option_plan_job import account_params

#: Smyšlené hodnoty, v logu se hledají jako text
ACCOUNT_USD = 13_579.0
MAX_LOSS_USD = 97.5


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.db'}",
    )
    return TestClient(create_app(settings))


def test_klice_uctu_jsou_zapisovatelne_a_engine_je_cte_stejne(client: TestClient) -> None:
    """Kladné číslo se uloží jako číslo (i z textu), engine z uložených hodnot čte totéž."""
    assert set(ACCOUNT_KEYS) <= WRITABLE_SETTINGS
    assert client.put(f"/settings/{ACCOUNT_KEY}", json={"value": ACCOUNT_USD}).json() == {
        "key": ACCOUNT_KEY,
        "value": ACCOUNT_USD,
    }
    saved = client.put(f"/settings/{MAX_LOSS_KEY}", json={"value": " 97.5 "})
    assert saved.status_code == 200 and saved.json()["value"] == MAX_LOSS_USD
    stored = client.get("/settings").json()["settings"]
    assert (stored[ACCOUNT_KEY], stored[MAX_LOSS_KEY]) == (ACCOUNT_USD, MAX_LOSS_USD)
    params = account_params({key: stored[key] for key in ACCOUNT_KEYS})
    assert (params.account_usd, params.max_loss_usd) == (ACCOUNT_USD, MAX_LOSS_USD)


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_prazdna_hodnota_je_nenastaveno(client: TestClient, empty: object) -> None:
    """Prázdná hodnota se uloží jako `null`; engine ji bere jako nenastavenou (K2 A)."""
    client.put(f"/settings/{ACCOUNT_KEY}", json={"value": ACCOUNT_USD})
    cleared = client.put(f"/settings/{ACCOUNT_KEY}", json={"value": empty})
    assert cleared.status_code == 200 and cleared.json()["value"] is None
    stored = client.get("/settings").json()["settings"]
    assert stored[ACCOUNT_KEY] is None
    assert account_params(stored).account_usd is None


@pytest.mark.parametrize(
    "bad", [0, -97.5, "abc", True, [97.5], {"usd": 97.5}, "nan", "inf", "-inf", "1e999"]
)
def test_neplatna_hodnota_422_a_nic_se_neulozi(client: TestClient, bad: object) -> None:
    client.put(f"/settings/{MAX_LOSS_KEY}", json={"value": MAX_LOSS_USD})
    response = client.put(f"/settings/{MAX_LOSS_KEY}", json={"value": bad})
    assert response.status_code == 422, bad
    assert "kladné číslo" in response.json()["detail"]
    assert client.get("/settings").json()["settings"][MAX_LOSS_KEY] == MAX_LOSS_USD


def test_hodnoty_uctu_nejdou_do_logu(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    """AC #1743: hodnota účtu nejde do logu API ani do hlášky chyby."""
    with caplog.at_level(logging.DEBUG):
        client.put(f"/settings/{ACCOUNT_KEY}", json={"value": ACCOUNT_USD})
        client.put(f"/settings/{MAX_LOSS_KEY}", json={"value": MAX_LOSS_USD})
        rejected = client.put(f"/settings/{MAX_LOSS_KEY}", json={"value": -86420.5})
        client.get("/settings")
    assert rejected.status_code == 422 and "86420" not in rejected.text
    # Bez mezer včetně nezlomitelných: „13 579“ i „13579“ jsou totéž číslo
    logged = re.sub(r"\s", "", caplog.text)
    for secret in ("13579", "97.5", "97,5", "86420"):
        assert secret not in logged, secret
