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
from sqlalchemy import text, update

from gexlens_api.briefing_routes import build_briefing_router
from gexlens_api.main import create_app
from gexlens_api.meta_repo import MetaRepository
from gexlens_engine.compute.settle import is_trading_session, us_open_ts
from gexlens_engine.config import Settings
from gexlens_engine.storage.briefing_verdicts_store import (
    BriefingVerdictRepository,
    VerdictOutcome,
)
from gexlens_engine.storage.meta import briefing_verdicts_table


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
    assert es["missing"] == []
    assert es["created_at"] and es["updated_at"]
    older = client.get("/briefing/verdicts", params={"days": 90}).json()["verdicts"]
    assert len(older) == 3


def test_vypis_doplni_sloupec_missing_kdyz_api_nabehne_pred_enginem(
    client: TestClient, meta: MetaRepository
) -> None:
    """Tabulka bez `missing` (engine ještě nenaběhl): výpis sloupec doplní, starý řádek = null."""
    _write(meta)
    with meta.engine().begin() as conn:
        conn.execute(text("ALTER TABLE briefing_verdicts DROP COLUMN missing"))
    (row,) = client.get("/briefing/verdicts", params={"symbol": "ES"}).json()["verdicts"]
    assert row["missing"] is None and row["verdict"] == "long"


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


def _evaluate(meta: MetaRepository, verdict_id: int, hit: bool) -> None:
    BriefingVerdictRepository(meta.engine()).update_outcome(
        verdict_id,
        VerdictOutcome(
            open=7000.0, us_open=7010.0, close=7040.0, move_pts=30.0, move_em=0.75, hit=hit
        ),
        dt.datetime.now(dt.UTC),
    )


@pytest.mark.parametrize(
    "session",
    [dt.date(2026, 10, 2), dt.date(2026, 11, 3)],  # US letní čas (open 13:30 UTC) a zimní (14:30)
)
def test_verdict_stats_jen_aktualni_verze_a_zapis_pred_openem(
    client: TestClient, meta: MetaRepository, session: dt.date
) -> None:
    """#1588 (Rozhodnuto 20. 10.: C a B): statistiky čtou jen aktuální verzi pravidel
    a vyřadí řádek zapsaný od US openu (řádky z Briefingu do 16. 10.); nic se nemaže."""
    from gexlens_engine.compute.dayverdict import VERDICT_RULES_VERSION

    days = (dt.datetime.now(dt.UTC).date() - session).days + 30
    _write(meta, session, symbol="ES", rules_version=VERDICT_RULES_VERSION)
    _write(meta, session, symbol="NQ", rules_version=VERDICT_RULES_VERSION)
    _write(meta, session, symbol="RTY", rules_version=VERDICT_RULES_VERSION - 1)
    rows = client.get("/briefing/verdicts", params={"days": days}).json()["verdicts"]
    ids = {row["symbol"]: row["id"] for row in rows}
    for verdict_id in ids.values():
        _evaluate(meta, verdict_id, hit=True)
    # NQ přepsaný Briefingem až v okamžiku openu (vzor id 1 z 9. 9.) — look-ahead
    with meta.engine().begin() as conn:
        conn.execute(
            update(briefing_verdicts_table)
            .where(briefing_verdicts_table.c.id == ids["NQ"])
            .values(updated_at=us_open_ts(session))
        )

    stats = client.get("/briefing/verdicts/stats", params={"days": days}).json()
    assert stats["rules_version"] == VERDICT_RULES_VERSION
    assert stats["evaluated"] == 1  # jen ES: NQ po openu, RTY starší verze
    assert stats["by_verdict"]["long"]["n"] == 1
    # Řádky zůstávají ve výpisu
    assert len(client.get("/briefing/verdicts", params={"days": days}).json()["verdicts"]) == 3
