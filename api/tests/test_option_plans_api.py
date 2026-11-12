"""Plán opce QQQ v API (F4a #1743, ADR-0062): seznam a detail plánu, parametry účtu.

Řádky `option_plans` mají tvar, který zapisuje engine (`option_plan_job`): plán z 9:15 ET,
přecenění v klíči `repricing`. Čísla účtu jsou smyšlená (skutečný účet do repa nepatří).
"""

import datetime as dt
import logging
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update

from gexlens_api.crud import WRITABLE_SETTINGS
from gexlens_api.main import create_app
from gexlens_engine.compute.option_plan import ACCOUNT_KEY, ACCOUNT_KEYS, MAX_LOSS_KEY
from gexlens_engine.config import Settings
from gexlens_engine.option_plan_job import account_params
from gexlens_engine.storage.option_plans_store import OptionPlansRepository, option_plans_table

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


@pytest.fixture
def plans(tmp_path: Path) -> OptionPlansRepository:
    """Tabulka v téže SQLite jako app, zapisuje ji jen test (v provozu engine)."""
    repository = OptionPlansRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'meta.db'}"))
    repository.ensure_schema()
    return repository


MONDAY = dt.date(2026, 11, 2)
TUESDAY = dt.date(2026, 11, 3)
AT_915 = dt.datetime(2026, 11, 2, 14, 15, tzinfo=dt.UTC)
SHADOWS = {"entry_b": {"rules": "stín vstup B"}, "wick_test": {"rules": "stín test knotem"}}


def _main(direction: str, strike: float, bid: float, slot_ts: str) -> dict[str, Any]:
    """Hlavní plán ve tvaru `plan_to_json` (jen pole, která test čte)."""
    right = "C" if direction == "long" else "P"
    return {
        "created_at": slot_ts,
        "direction": direction,
        "right": right,
        "rules": "hlavní",
        "variants": [
            {"role": "hlavní", "right": right, "strike": strike, "bid": bid, "ask": bid + 0.04},
            {"role": "záložní", "right": right, "strike": strike - 1, "bid": bid - 0.2},
        ],
        "verdict": "čeká — čeká na vstup A (flip 697,65)",
    }


def _built(direction: str, strike: float) -> dict[str, Any]:
    return {
        "slot": "9:15",
        "reasons": [],
        "levels_nq": {"flip": 28_820.0},
        "chain": {"source": "tastytrade", "complete": True},
        "main": _main(direction, strike, 1.30, AT_915.isoformat()),
        "shadows": SHADOWS,
    }


def _create(plans: OptionPlansRepository, scenario_id: int, day: dt.date, **row: Any) -> int:
    plan_id = plans.create(
        scenario_id=scenario_id,
        day=day,
        created_at=dt.datetime.combine(day, AT_915.timetz()),
        rules_version=1,
        **row,
    )
    assert plan_id is not None
    return plan_id


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


def test_prazdna_db_prazdny_seznam_a_404(client: TestClient) -> None:
    assert client.get("/option-plans").json() == {"option_plans": []}
    missing = client.get("/option-plans/1")
    assert missing.status_code == 404 and "neexistuje" in missing.json()["detail"]
    assert client.get("/option-plans?limit=0").status_code == 422
    assert client.get("/option-plans?limit=501").status_code == 422


def test_plan_short_a_long_seznam_a_detail(
    client: TestClient, plans: OptionPlansRepository, tmp_path: Path
) -> None:
    """Short z 9:15 přeceněný v 9:31 (ceny vedle) a long před přeceněním; seznam bez JSON."""
    prices = {"main": [{"role": "hlavní", "strike": 687.0, "bid": 1.22, "ask": 1.26}]}
    short = _built("short", 687.0) | {
        "repricing": {"slot": "9:31", "reasons": [], "chain": {}, "prices": prices, "plan": None}
    }
    short_id = _create(plans, 35, MONDAY, verdict="čeká", status="sestaven", plan=short)
    long_plan = _built("long", 700.0)
    long_id = _create(plans, 36, TUESDAY, verdict="čeká", status="sestaven", plan=long_plan)
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'meta.db'}")
    with db.begin() as conn:  # obrázek doplní F4b #1744
        conn.execute(
            update(option_plans_table)
            .where(option_plans_table.c.id == short_id)
            .values(image_path="option_plans/QQQ/2026-11-02/1.png")
        )
    listed = client.get("/option-plans").json()["option_plans"]
    assert [item["id"] for item in listed] == [long_id, short_id]  # nejnovější seance první
    newest = client.get("/option-plans?limit=1").json()["option_plans"]
    assert [item["id"] for item in newest] == [long_id]
    first, second = listed
    assert "plan" not in first and "plan" not in second  # celý JSON jen v detailu (B)
    assert (first["day"], first["status"], first["verdict"]) == ("2026-11-03", "sestaven", "čeká")
    assert first["effective"]["main"]["right"] == "C" and first["effective"]["slot"] == "9:15"
    assert (first["effective"]["prices"], first["effective"]["repricing_reasons"]) == (None, None)
    assert (first["has_image"], second["has_image"]) == (False, True)
    assert second["effective"]["main"]["right"] == "P"
    assert second["effective"]["prices"] == prices
    assert second["effective"]["repricing_reasons"] == []
    assert second["created_at"] == AT_915.isoformat()
    assert (second["evaluated_at"], second["result"]) == (None, None)
    detail = client.get(f"/option-plans/{short_id}").json()
    assert detail["plan"] == short
    assert {key: value for key, value in detail.items() if key != "plan"} == second


def test_plan_sestaveny_v_9_31_plati_z_9_31(
    client: TestClient, plans: OptionPlansRepository
) -> None:
    """Plán bez opce z 9:15 sestavený celý v 9:31 (K4 bod 10): platný je `repricing.plan`."""
    rebuilt = {
        "slot": "9:31",
        "levels_nq": {"flip": 28_790.0},
        "main": _main("short", 686.0, 1.10, "2026-11-02T14:31:00+00:00"),
        "shadows": SHADOWS,
    }
    stored = {
        "slot": "9:15",
        "reasons": ["ranní snímek 9:15 ET nedorazil do 14:16:40 UTC"],
        "levels_nq": {"flip": 28_820.0},
        "chain": None,
        "main": None,
        "shadows": {},
        "repricing": {"slot": "9:31", "reasons": [], "chain": {}, "prices": None, "plan": rebuilt},
    }
    # Sloupce popisují plán z 9:31 (Rozhodnuto 11. 11.: A), JSON z 9:15 zůstal
    plan_id = _create(plans, 37, MONDAY, verdict="neobchodovat", status="sestaven", plan=stored)
    (item,) = client.get("/option-plans").json()["option_plans"]
    effective = item["effective"]
    assert (item["status"], item["verdict"]) == ("sestaven", "neobchodovat")
    assert effective["slot"] == "9:31" and effective["main"] == rebuilt["main"]
    assert effective["shadows"] == SHADOWS and effective["levels_nq"] == {"flip": 28_790.0}
    assert effective["prices"] is None and effective["repricing_reasons"] == []
    assert effective["reasons"] == stored["reasons"]
    detail = client.get(f"/option-plans/{plan_id}").json()
    assert detail["plan"]["main"] is None and detail["plan"]["repricing"]["plan"] == rebuilt


def test_plan_bez_opce_po_obou_snimcich(client: TestClient, plans: OptionPlansRepository) -> None:
    """Bez opce v 9:15 i v 9:31: `main` null, důvody obou okamžiků zvlášť."""
    gap_915 = ["snímek 9:15 ET neúplný: 1DTE bez kotace"]
    gap_931 = ["úrovně NQ minuty 9:31 ET nedorazily do 14:34:00 UTC"]
    stored = {
        "slot": "9:15",
        "reasons": gap_915,
        "levels_nq": {"flip": 28_820.0},
        "chain": {"complete": False},
        "main": None,
        "shadows": {},
        "repricing": {
            "slot": "9:31",
            "reasons": gap_931,
            "chain": {},
            "prices": None,
            "plan": None,
        },
    }
    plan_id = _create(plans, 38, MONDAY, verdict=None, status="bez opce", plan=stored)
    effective = client.get(f"/option-plans/{plan_id}").json()["effective"]
    assert (effective["slot"], effective["main"], effective["shadows"]) == ("9:15", None, {})
    assert (effective["reasons"], effective["repricing_reasons"]) == (gap_915, gap_931)
