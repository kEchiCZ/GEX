"""Verdikt dne (#1090): API jen čte — výpis per symbol a okno dní, track record.

Zapisuje engine (`verdict_writer`, #1595); stráž od US openu (#1585) testuje
engine (`test_settle`, `test_briefing_verdicts`, `test_verdict_writer`).
"""

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gexlens_api.briefing_routes import build_briefing_router
from gexlens_api.main import create_app
from gexlens_api.meta_repo import MetaRepository
from gexlens_engine.compute.settle import is_trading_session, us_open_ts
from gexlens_engine.config import Settings
from gexlens_engine.storage.briefing_verdicts_store import (
    BriefingVerdictRepository,
    VerdictOutcome,
)


def _trading_day_on_or_before(day: dt.date) -> dt.date:
    while not is_trading_session(day):
        day -= dt.timedelta(days=1)
    return day


# Verdikt jen pro obchodní den (#1309) — o víkendu testy berou poslední pátek.
# Výpis a statistiky počítají okno dní od skutečného dneška, proto „dnešní" seance.
TODAY = _trading_day_on_or_before(dt.datetime.now(dt.UTC).date())

VOTES = [
    {"name": "trend_higher", "vote": 2, "reason": "týden i den rostoucí"},
    {"name": "gamma", "vote": 1, "reason": "negativní gamma = momentum"},
]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}",
    )


@pytest.fixture
def meta(tmp_path: Path) -> MetaRepository:
    return MetaRepository(_settings(tmp_path))


@pytest.fixture
def client(meta: MetaRepository) -> TestClient:
    app = FastAPI()
    app.include_router(build_briefing_router(meta))
    return TestClient(app)


def _write(meta: MetaRepository, session: dt.date = TODAY, **overrides: Any) -> None:
    """Zápis cestou enginu — hodinu před US openem seance (stráž projde)."""
    repository = BriefingVerdictRepository(
        meta.engine(), clock=lambda: us_open_ts(session) - dt.timedelta(hours=1)
    )
    repository.ensure_schema()
    values: dict[str, Any] = {
        "symbol": "ES",
        "verdict": "long",
        "score": 4,
        "votes": VOTES,
        "rules_version": 3,
    }
    values.update(overrides)
    repository.upsert_verdict(session_date=session, **values)


def test_verdicts_per_symbol_a_okno_dnu(client: TestClient, meta: MetaRepository) -> None:
    _write(meta)
    _write(meta, symbol="NQ", verdict="short", score=-3)
    old_day = _trading_day_on_or_before(TODAY - dt.timedelta(days=60))
    _write(meta, old_day, verdict="wait_news", score=0)

    all_recent = client.get("/briefing/verdicts", params={"days": 30}).json()["verdicts"]
    assert {row["symbol"] for row in all_recent} == {"ES", "NQ"}
    nq = client.get("/briefing/verdicts", params={"symbol": "NQ"}).json()["verdicts"]
    assert [row["verdict"] for row in nq] == ["short"]
    (es,) = client.get("/briefing/verdicts", params={"symbol": "ES"}).json()["verdicts"]
    assert es["session_date"] == TODAY.isoformat() and es["votes"] == VOTES
    assert es["created_at"] and es["updated_at"]
    older = client.get("/briefing/verdicts", params={"days": 90}).json()["verdicts"]
    assert len(older) == 3


@pytest.mark.parametrize("factory", ["router", "create_app"])
def test_zapis_verdiktu_z_api_neexistuje(
    tmp_path: Path, meta: MetaRepository, client: TestClient, factory: str
) -> None:
    """#1595 c1: zapisuje jen engine — starý bundle s POST dostane 405 a nic se neuloží."""
    payload = {
        "session_date": TODAY.isoformat(),
        "symbol": "ES",
        "verdict": "long",
        "score": 4,
        "votes": VOTES,
        "rules_version": 3,
    }
    target = client if factory == "router" else TestClient(create_app(_settings(tmp_path)))
    assert target.post("/briefing/verdicts", json=payload).status_code == 405
    assert target.get("/briefing/verdicts", params={"days": 400}).json()["verdicts"] == []


def test_verdict_stats_prazdne_a_po_vyhodnoceni(client: TestClient, meta: MetaRepository) -> None:
    """#1091: statistiky bez výsledků jsou prázdné; s výsledkem počítají hit-rate."""
    empty = client.get("/briefing/verdicts/stats").json()
    assert empty["evaluated"] == 0 and empty["by_verdict"] == {} and empty["min_samples"] == 30

    _write(meta)
    (created,) = client.get("/briefing/verdicts", params={"symbol": "ES"}).json()["verdicts"]
    repo = BriefingVerdictRepository(meta.engine())
    repo.update_outcome(
        created["id"],
        VerdictOutcome(
            open=7000.0, us_open=7010.0, close=7040.0, move_pts=30.0, move_em=0.75, hit=True
        ),
        dt.datetime.now(dt.UTC),
    )
    stats = client.get("/briefing/verdicts/stats", params={"symbol": "ES"}).json()
    assert stats["evaluated"] == 1
    assert stats["by_verdict"]["long"]["hits"] == 1
    assert stats["by_vote"]["trend_higher"] == {"n": 1, "hits": 1, "hit_rate": 1.0, "wilson_lb": pytest.approx(0.207, abs=0.01), "gate_open": False}  # fmt: skip  # noqa: E501
    assert client.get("/briefing/verdicts/stats", params={"symbol": "NQ"}).json()["evaluated"] == 0
