"""Verdikt dne z Briefingu (#1090): upsert per seance × symbol, výpis, validace,
zmrazení od US openu (#1585)."""

import datetime as dt
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gexlens_api.briefing_routes import build_briefing_router
from gexlens_api.main import create_app
from gexlens_api.meta_repo import MetaRepository
from gexlens_engine.compute.settle import is_trading_session, settle_ts, us_open_ts
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


class Clock:
    """Hodiny routeru (#1585) — testy posouvají čas kolem US openu seance."""

    def __init__(self, at: dt.datetime) -> None:
        self.at = at

    def __call__(self) -> dt.datetime:
        return self.at


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}",
    )


@pytest.fixture
def meta(tmp_path: Path) -> MetaRepository:
    return MetaRepository(_settings(tmp_path))


@pytest.fixture
def clock() -> Clock:
    # Hodinu před US openem dnešní seance — zápis smí projít
    return Clock(us_open_ts(TODAY) - dt.timedelta(hours=1))


@pytest.fixture
def client(meta: MetaRepository, clock: Clock) -> TestClient:
    app = FastAPI()
    app.include_router(build_briefing_router(meta, now=clock))
    return TestClient(app)


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


def _utc(value: object) -> dt.datetime:
    """ISO čas z odpovědi → UTC (SQLite vrací naivní, PostgreSQL s pásmem)."""
    parsed = dt.datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed.astimezone(dt.UTC)


def _stored(client: TestClient, symbol: str = "ES") -> list[dict[str, object]]:
    rows: list[dict[str, object]] = client.get(
        "/briefing/verdicts", params={"symbol": symbol, "days": 400}
    ).json()["verdicts"]
    return rows


def test_verdict_upsert_je_idempotentni(client: TestClient, clock: Clock) -> None:
    first = client.post("/briefing/verdicts", json=_payload())
    assert first.status_code == 201
    created = first.json()
    assert created["verdict"] == "long"
    assert created["votes"][0]["name"] == "trend_higher"
    # Uložený čas = okamžik, který stráž ověřila (jeden zdroj času, #1585)
    assert _utc(created["updated_at"]) == clock.at

    # Před openem se verdikt může změnit — stejná seance a symbol přepíše, nezdvojí
    clock.at = us_open_ts(TODAY) - dt.timedelta(seconds=1)
    second = client.post("/briefing/verdicts", json=_payload(verdict="none", score=1))
    assert second.status_code == 201
    assert second.json()["id"] == created["id"]
    assert second.json()["verdict"] == "none"

    listed = client.get("/briefing/verdicts", params={"symbol": "ES"}).json()["verdicts"]
    assert len(listed) == 1
    assert listed[0]["score"] == 1
    assert listed[0]["session_date"] == TODAY.isoformat()


def test_verdicts_per_symbol_a_okno_dnu(client: TestClient, clock: Clock) -> None:
    client.post("/briefing/verdicts", json=_payload())
    client.post("/briefing/verdicts", json=_payload(symbol="NQ", verdict="short", score=-3))
    old_day = _trading_day_on_or_before(TODAY - dt.timedelta(days=60))
    clock.at = us_open_ts(old_day) - dt.timedelta(hours=2)
    stored_old = client.post(
        "/briefing/verdicts", json=_payload(session_date=old_day.isoformat(), verdict="wait_news")
    )
    assert stored_old.status_code == 201

    all_recent = client.get("/briefing/verdicts", params={"days": 30}).json()["verdicts"]
    assert {row["symbol"] for row in all_recent} == {"ES", "NQ"}
    nq = client.get("/briefing/verdicts", params={"symbol": "NQ"}).json()["verdicts"]
    assert [row["verdict"] for row in nq] == ["short"]
    older = client.get("/briefing/verdicts", params={"days": 90}).json()["verdicts"]
    assert len(older) == 3


def test_verdict_validace(client: TestClient) -> None:
    assert client.post("/briefing/verdicts", json=_payload(verdict="maybe")).status_code == 422
    assert client.post("/briefing/verdicts", json=_payload(score=99)).status_code == 422


def test_verdict_vikend_odmitne(client: TestClient, clock: Clock) -> None:
    """#1309: sobota ani neděle nemají US open ani settle — verdikt by visel
    nevyhodnocený navždy. Briefing ho o víkendu neposílá, API ho odmítne."""
    # Neděle 27. 9. 22:05 UTC: Globex už otevřel (17:00 CDT), běží seance pondělí
    clock.at = dt.datetime(2026, 9, 27, 22, 5, tzinfo=dt.UTC)
    for weekend in ("2026-09-26", "2026-09-27"):
        rejected = client.post("/briefing/verdicts", json=_payload(session_date=weekend))
        assert rejected.status_code == 422 and "obchodní den" in rejected.json()["detail"]
    # Regrese obchodního dne: verdikt pondělí po nedělním otevření Globexu projde
    ok = client.post("/briefing/verdicts", json=_payload(session_date="2026-09-28"))
    assert ok.status_code == 201


def test_verdict_od_us_openu_se_neprepise(client: TestClient, clock: Clock) -> None:
    """#1585: od US openu se hodnotí pohyb open → settle — přepis by do předpovědi
    pustil hodnocený pohyb (id 2 na produkci: přepis 32 min po openu). Odmítnutí
    je 409 s důvodem a uložený řádek zůstane beze změny."""
    open_ts = us_open_ts(TODAY)
    clock.at = open_ts - dt.timedelta(minutes=10)
    created = client.post("/briefing/verdicts", json=_payload()).json()

    for moment in (
        open_ts,  # okamžik openu už patří hodnocenému pohybu
        open_ts + dt.timedelta(seconds=19),  # id 1: přepis 19 s po openu
        open_ts + dt.timedelta(minutes=32),  # id 2
        settle_ts(TODAY) + dt.timedelta(minutes=25),  # id 4: přepis po settle
    ):
        clock.at = moment
        rejected = client.post("/briefing/verdicts", json=_payload(verdict="short", score=-5))
        assert rejected.status_code == 409, moment
        detail = rejected.json()["detail"]
        assert "US openu" in detail and TODAY.isoformat() in detail

    [row] = _stored(client)
    assert row["id"] == created["id"]
    assert (row["verdict"], row["score"]) == ("long", 4)
    assert row["updated_at"] == created["updated_at"]


def test_verdict_po_doplneni_vysledku_nejde_prepsat(
    client: TestClient, clock: Clock, meta: MetaRepository
) -> None:
    """Riziko z #1585: přepis po doplnění výsledku by změnil verdikt, ale `outcome_hit`
    by zůstal spočtený pro předchozí. Stráž ho odmítne a výsledek patří uloženému."""
    created = client.post("/briefing/verdicts", json=_payload()).json()
    repo = BriefingVerdictRepository(meta.engine())
    repo.ensure_schema()
    evaluated_at = settle_ts(TODAY) + dt.timedelta(minutes=15)
    repo.update_outcome(
        int(created["id"]),
        VerdictOutcome(
            open=7000.0, us_open=7010.0, close=7040.0, move_pts=30.0, move_em=0.75, hit=True
        ),
        evaluated_at,
    )

    clock.at = evaluated_at + dt.timedelta(minutes=5)
    assert client.post("/briefing/verdicts", json=_payload(verdict="short")).status_code == 409
    [row] = _stored(client)
    assert (row["verdict"], row["outcome_hit"]) == ("long", True)


def test_verdict_pozde_zalozeny_po_openu_nevznikne(client: TestClient, clock: Clock) -> None:
    """#1585: verdikt poprvé spočítaný až po openu (id 7, 11 na produkci) je
    předpověď s look-ahead — nezaloží se, řádek seance nevznikne."""
    for moment in (
        us_open_ts(TODAY) + dt.timedelta(minutes=2),  # id 11
        settle_ts(TODAY) - dt.timedelta(minutes=50),  # id 7
    ):
        clock.at = moment
        assert client.post("/briefing/verdicts", json=_payload()).status_code == 409
    assert _stored(client) == []


def test_verdict_pondeli_proti_patku(client: TestClient, clock: Clock) -> None:
    """V pondělí ráno před openem jde zapsat jen pondělí — páteční seance je od
    pátečního openu zmrazená, víkend mezi nimi nic neotevírá."""
    clock.at = dt.datetime(2026, 10, 5, 13, 0, tzinfo=dt.UTC)  # po 5. 10. ráno, open 13:30 UTC
    friday = client.post("/briefing/verdicts", json=_payload(session_date="2026-10-02"))
    assert friday.status_code == 409
    monday = client.post("/briefing/verdicts", json=_payload(session_date="2026-10-05"))
    assert monday.status_code == 201
    clock.at = dt.datetime(2026, 10, 5, 13, 30, tzinfo=dt.UTC)
    late = client.post("/briefing/verdicts", json=_payload(session_date="2026-10-05", score=-4))
    assert late.status_code == 409


def test_verdict_svatek_a_zkracena_seance(client: TestClient, clock: Clock) -> None:
    """Svátek NYSE (Thanksgiving 26. 11. 2026) nemá US open — 422 jako víkend
    (#1308). Zkrácená seance den poté otevírá v 9:30 ET (14:30 UTC, EST) jako
    každý den; settle už ve 13:00 ET."""
    clock.at = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)
    holiday = client.post("/briefing/verdicts", json=_payload(session_date="2026-11-26"))
    assert holiday.status_code == 422

    early = "2026-11-27"
    clock.at = dt.datetime(2026, 11, 27, 14, 29, 59, tzinfo=dt.UTC)
    assert client.post("/briefing/verdicts", json=_payload(session_date=early)).status_code == 201
    for moment in (
        dt.datetime(2026, 11, 27, 14, 30, tzinfo=dt.UTC),
        dt.datetime(2026, 11, 27, 18, 20, tzinfo=dt.UTC),  # po settle 13:00 ET
    ):
        clock.at = moment
        rejected = client.post("/briefing/verdicts", json=_payload(session_date=early, score=-4))
        assert rejected.status_code == 409


@pytest.mark.parametrize(
    ("session", "open_utc"),
    [
        (dt.date(2026, 9, 15), dt.time(13, 30)),  # EDT
        # Evropa už má zimní čas (od 25. 10.), USA ještě letní — open dál 13:30 UTC
        (dt.date(2026, 10, 30), dt.time(13, 30)),
        (dt.date(2026, 11, 2), dt.time(14, 30)),  # EST od 1. 11.
        (dt.date(2026, 1, 15), dt.time(14, 30)),  # EST
    ],
)
def test_verdict_hranice_openu_letni_a_zimni_cas(
    client: TestClient, clock: Clock, session: dt.date, open_utc: dt.time
) -> None:
    """US open 9:30 ET padá v létě na 13:30 a v zimě na 14:30 UTC — pevná UTC
    hodina by se půl roku míjela (vzor #511)."""
    open_ts = dt.datetime.combine(session, open_utc, tzinfo=dt.UTC)
    clock.at = open_ts - dt.timedelta(seconds=1)
    before = client.post("/briefing/verdicts", json=_payload(session_date=session.isoformat()))
    assert before.status_code == 201
    clock.at = open_ts
    after = client.post("/briefing/verdicts", json=_payload(session_date=session.isoformat()))
    assert after.status_code == 409
    assert f"{open_utc:%H:%M} UTC" in after.json()["detail"]


def test_create_app_hlida_open_skutecnymi_hodinami(tmp_path: Path) -> None:
    """Zapojení v `create_app`: výchozí hodiny routeru jsou skutečný čas — seance
    z minulosti (15. 9. 2026) je dávno po openu."""
    app_client = TestClient(create_app(_settings(tmp_path)))
    rejected = app_client.post("/briefing/verdicts", json=_payload(session_date="2026-09-15"))
    assert rejected.status_code == 409


def test_verdict_stats_prazdne_a_po_vyhodnoceni(client: TestClient, meta: MetaRepository) -> None:
    """#1091: statistiky bez výsledků jsou prázdné; s výsledkem počítají hit-rate."""
    empty = client.get("/briefing/verdicts/stats").json()
    assert empty["evaluated"] == 0 and empty["by_verdict"] == {} and empty["min_samples"] == 30

    created = client.post("/briefing/verdicts", json=_payload()).json()
    repo = BriefingVerdictRepository(meta.engine())
    repo.ensure_schema()
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
