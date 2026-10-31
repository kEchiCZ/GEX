"""Verdikt dne zapisuje engine (#1595): okno před openem, upsert, stráž, neúplný výpočet."""

import asyncio
import datetime as dt
import math
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from test_scenario_auto import OPEN, SESSION, FakeApi, _runtime, _verdict_api

from gexlens_engine.compute.settle import session_bounds, us_open_ts
from gexlens_engine.scenario_auto import ScenarioGenerator
from gexlens_engine.storage.briefing_verdicts_store import BriefingVerdictRepository
from gexlens_engine.storage.meta import briefing_verdicts_table
from gexlens_engine.storage.scenarios_store import ScenariosRepository
from gexlens_engine.verdict_writer import VERDICT_WINDOW_MINUTES, DayVerdictWriter

PREV_DAY = dt.date(2026, 9, 14)


def _full_api(session: dt.date = SESSION, prev_day: dt.date = PREV_DAY) -> FakeApi:
    """Úplné vstupy (i útes gammy) — žádné čtení nesmí selhat, jinak je výpočet neúplný."""
    api = _verdict_api(session, prev_day)
    api.routes["/gammacliff/ES"] = {"rows": [{"cliff_share": 0.2}]}
    return api


class _Clock:
    """Skutečný čas zápisu — v testu ho posouvá test, ne minuta cyklu."""

    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


def _writer(tmp_path: Path, api: FakeApi, clock: _Clock) -> DayVerdictWriter:
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}")
    repository = BriefingVerdictRepository(db, clock=clock)
    repository.ensure_schema()
    return DayVerdictWriter("ES", repository, api)  # type: ignore[arg-type]


def _rows(writer: DayVerdictWriter) -> list[dict[str, Any]]:
    with writer.repository._engine.connect() as conn:
        return [dict(row._mapping) for row in conn.execute(select(briefing_verdicts_table))]


def _minute(writer: DayVerdictWriter, clock: _Clock, now: dt.datetime, spot: float) -> Any:
    clock.now = now + dt.timedelta(seconds=5)  # zápis pár sekund po začátku minuty
    return asyncio.run(writer.on_minute(now, spot, _runtime(SESSION)))


def test_kazdou_minutu_okna_prepise_radek_plati_posledni_pred_openem(tmp_path: Path) -> None:
    clock = _Clock(OPEN)
    api = _full_api()
    writer = _writer(tmp_path, api, clock)
    # Minuta před oknem mimo pětiminutový rytmus: nic a API se nečte
    assert _minute(writer, clock, OPEN - dt.timedelta(minutes=16), 7590.0) is None
    assert _rows(writer) == [] and api.calls == []
    # První minuta okna: SHORT (cena 7590 pod PDC 7632 = RTH close předchozího dne)
    first = _minute(writer, clock, OPEN - dt.timedelta(minutes=VERDICT_WINDOW_MINUTES), 7590.0)
    assert first is not None and first.complete and first.verdict.verdict == "short"
    (row,) = _rows(writer)
    # Verze 4 od bumpu #1403 jen značí zlom vstupu (CumΔ z tisků, spot se stářím),
    # pravidla jsou táž jako ve verzi 3 (ADR-0057 bod 8)
    assert row["verdict"] == "short" and row["rules_version"] == 4
    overnight = next(vote for vote in row["votes"] if vote["name"] == "overnight")
    assert overnight == {
        "name": "overnight",
        "vote": -1,
        "reason": "cena 7590 pod včerejším close 7632",
    }
    created = row["created_at"]
    # Poslední minuta před openem: cena nad PDC → jiné skóre; týž řádek, created_at drží
    last = _minute(writer, clock, OPEN - dt.timedelta(minutes=1), 7640.25)
    assert last is not None
    (row,) = _rows(writer)
    assert row["score"] == last.verdict.score == first.verdict.score + 2
    assert row["created_at"] == created and row["updated_at"] > created
    assert "cena 7640.25 nad včerejším close 7632" in [vote["reason"] for vote in row["votes"]]
    # Od openu se nezapisuje a API se nečte
    calls = len(api.calls)
    assert _minute(writer, clock, OPEN, 7700.0) is None
    assert len(api.calls) == calls
    (after,) = _rows(writer)
    assert after["score"] == row["score"] and after["updated_at"] == row["updated_at"]


def test_od_zacatku_seance_kazdych_5_min_pak_kazdou_minutu(tmp_path: Path) -> None:
    """Rozhodnutí a1: od 17:00 CT předchozího dne každých 5 min, od open − 15 min každou minutu."""
    clock = _Clock(OPEN)
    api = _full_api()
    writer = _writer(tmp_path, api, clock)
    start, _ = session_bounds(SESSION)
    assert start == dt.datetime(2026, 9, 14, 22, 0, tzinfo=dt.UTC)  # 17:00 CDT
    # Minuta před začátkem patří předchozí seanci, ta je už po openu
    assert _minute(writer, clock, start - dt.timedelta(minutes=5), 7590.0) is None
    assert _rows(writer) == [] and api.calls == []
    written = []
    for now in (
        start,
        start + dt.timedelta(minutes=1),
        dt.datetime(2026, 9, 15, 3, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 9, 15, 3, 4, tzinfo=dt.UTC),
        OPEN - dt.timedelta(minutes=20),
        OPEN - dt.timedelta(minutes=19),
        OPEN - dt.timedelta(minutes=15),
        OPEN - dt.timedelta(minutes=14),
    ):
        calls = len(api.calls)
        if _minute(writer, clock, now, 7590.0) is not None:
            written.append(f"{now:%H:%M}")
        else:
            assert len(api.calls) == calls  # minuta bez výpočtu API nečte
    assert written == ["22:00", "03:00", "13:10", "13:15", "13:16"]
    (row,) = _rows(writer)
    # SQLite vrací čas bez pásma (uložené UTC); zápis 5 s po začátku minuty
    assert row["created_at"] == (start + dt.timedelta(seconds=5)).replace(tzinfo=None)
    last = OPEN - dt.timedelta(minutes=14) + dt.timedelta(seconds=5)
    assert row["updated_at"] == last.replace(tzinfo=None)


def test_scenar_prevezme_verdikt_zapisovace_api_se_cte_jednou(tmp_path: Path) -> None:
    """Rozhodnutí d1: scénář téže minuty vznikne z uloženého verdiktu bez druhého čtení API."""
    clock = _Clock(OPEN)
    api = _full_api()
    writer = _writer(tmp_path, api, clock)
    scenarios = ScenariosRepository(writer.repository._engine)
    scenarios.ensure_schema()
    generator = ScenarioGenerator("ES", scenarios, minutes_before_open=15)
    now = OPEN - dt.timedelta(minutes=VERDICT_WINDOW_MINUTES)
    pre_open = _minute(writer, clock, now, 7590.0)
    calls = len(api.calls)
    asyncio.run(generator.on_minute(now, pre_open, _runtime(SESSION)))
    assert len(api.calls) == calls
    (scenario,) = scenarios.list_for("ES")
    (row,) = _rows(writer)
    assert scenario.rationale is not None
    assert scenario.rationale["verdict"] == row["verdict"] == "short"
    assert scenario.rationale["score"] == row["score"] and scenario.entry == 7590.0


def test_straz_skutecnym_casem_zapisu_cyklus_dobehl_po_openu(tmp_path: Path) -> None:
    """Minuta 9:29 ET, ale zápis až po 9:30:00 (pipeline běží za sebou) — nezapíše se."""
    clock = _Clock(OPEN)
    writer = _writer(tmp_path, _full_api(), clock)
    _minute(writer, clock, OPEN - dt.timedelta(minutes=2), 7590.0)
    (before,) = _rows(writer)
    clock.now = OPEN + dt.timedelta(seconds=1)
    result = asyncio.run(
        writer.on_minute(OPEN - dt.timedelta(minutes=1), 7640.0, _runtime(SESSION))
    )
    assert result is None
    (after,) = _rows(writer)
    assert after["updated_at"] == before["updated_at"] and after["score"] == before["score"]
    # SQLite vrací čas bez pásma (uložené UTC)
    assert after["updated_at"].replace(tzinfo=dt.UTC) < us_open_ts(SESSION)


def test_neuplny_vypocet_neprepise_uplny_radek(tmp_path: Path) -> None:
    clock = _Clock(OPEN)
    writer = _writer(tmp_path, _full_api(), clock)
    assert _minute(writer, clock, OPEN - dt.timedelta(minutes=3), 7590.0).complete
    (complete,) = _rows(writer)
    # Výpadek API v 9:28: hlasovat by mohly jen tendence a gamma → „none" s důvody „bez dat"
    writer.api = FakeApi({"/news/upcoming": {"upcoming": []}})  # type: ignore[assignment]
    degraded = _minute(writer, clock, OPEN - dt.timedelta(minutes=2), 7590.0)
    assert degraded is not None and not degraded.complete
    assert degraded.verdict.verdict == "none"
    (row,) = _rows(writer)
    assert row["verdict"] == complete["verdict"] == "short"
    assert row["updated_at"] == complete["updated_at"]
    # Restart enginu v okně: runtime ještě bez tendence — taky neúplný, nepřepíše
    writer.api = _full_api()  # type: ignore[assignment]
    runtime = _runtime(SESSION)
    runtime.tendency_band = None
    clock.now = OPEN - dt.timedelta(seconds=50)
    restarted = asyncio.run(writer.on_minute(OPEN - dt.timedelta(minutes=1), 7590.0, runtime))
    assert restarted is not None and not restarted.complete
    (row,) = _rows(writer)
    assert row["updated_at"] == complete["updated_at"]


def test_bez_tendence_v_pipeline_je_vypocet_uplny(tmp_path: Path) -> None:
    """GEXLENS_TENDENCY_ENABLED=false: tendence nehlasuje, ale výpočet je úplný a přepisuje."""
    clock = _Clock(OPEN)
    writer = _writer(tmp_path, _full_api(), clock)
    writer.tendency_required = False
    runtime = _runtime(SESSION)
    runtime.tendency_band = None
    for minutes, spot in ((3, 7590.0), (2, 7640.25)):
        now = OPEN - dt.timedelta(minutes=minutes)
        clock.now = now + dt.timedelta(seconds=5)
        result = asyncio.run(writer.on_minute(now, spot, runtime))
        assert result is not None and result.complete
    (row,) = _rows(writer)
    assert "cena 7640.25 nad včerejším close 7632" in [vote["reason"] for vote in row["votes"]]


def test_neuplny_vypocet_zalozi_radek_kdyz_zadny_neni(tmp_path: Path) -> None:
    """Mezera má být vidět: bez úplného výpočtu se uloží, co engine spočítal, s důvody."""
    clock = _Clock(OPEN)
    writer = _writer(tmp_path, FakeApi({"/news/upcoming": {"upcoming": []}}), clock)
    result = _minute(writer, clock, OPEN - dt.timedelta(minutes=5), 7590.0)
    assert result is not None and not result.complete
    (row,) = _rows(writer)
    assert row["verdict"] == "none"
    reasons = [vote["reason"] for vote in row["votes"]]
    assert "vyšší TF bez dat" in reasons and "overnight vs. včerejší close bez dat" in reasons
    # Neúplnost je v řádku vidět (#1595 e1): selhaná čtení s důvodem
    assert {"candles W: RuntimeError", "bars dnes: RuntimeError"} <= set(row["missing"])


def test_soustavny_vypadek_vstupu_obnovuje_neuplny_radek(tmp_path: Path) -> None:
    """#1595 e1: jeden vstup selhává celou noc — řádek se dál obnovuje (platí poslední
    stav), nese seznam chybějících vstupů; úplný výpočet ho pak přepíše."""
    clock = _Clock(OPEN)
    api = _full_api()
    del api.routes["/gammacliff/ES"]
    writer = _writer(tmp_path, api, clock)
    start = dt.datetime(2026, 9, 14, 22, 0, tzinfo=dt.UTC)  # začátek seance
    assert not _minute(writer, clock, start, 7590.0).complete
    last = _minute(writer, clock, OPEN - dt.timedelta(minutes=1), 7640.25)
    assert last is not None and last.missing == ("gamma útes: RuntimeError",)
    (row,) = _rows(writer)
    assert row["missing"] == ["gamma útes: RuntimeError"]
    assert "cena 7640.25 nad včerejším close 7632" in [vote["reason"] for vote in row["votes"]]
    assert row["updated_at"] > row["created_at"]


def test_bez_platne_ceny_se_minuta_nezapise(tmp_path: Path) -> None:
    clock = _Clock(OPEN)
    api = _full_api()
    writer = _writer(tmp_path, api, clock)
    for spot in (math.nan, 0.0, -1.0):
        assert _minute(writer, clock, OPEN - dt.timedelta(minutes=5), spot) is None
    assert _rows(writer) == [] and api.calls == []


def test_v6_bez_zive_ceny_neuplny_radek_s_duvodem_a_scenar_nevznikne(tmp_path: Path) -> None:
    """#1600 (pod v6): pipeline předá místo staré ceny None — hlas overnight „bez dat“,
    řádek neúplný s důvodem a stářím spotu; první minuta s živou cenou ho přepíše."""
    clock = _Clock(OPEN)
    writer = _writer(tmp_path, _full_api(), clock)
    now = OPEN - dt.timedelta(minutes=5)
    clock.now = now + dt.timedelta(seconds=5)
    reason = "spot: poslední cena 7590 (bar) stará 240 s"
    stale = asyncio.run(writer.on_minute(now, None, _runtime(SESSION), reason))
    assert stale is not None and not stale.complete and stale.spot is None
    (row,) = _rows(writer)
    assert row["missing"] == [reason]
    overnight = next(vote for vote in row["votes"] if vote["name"] == "overnight")
    assert overnight["vote"] == 0 and overnight["reason"] == "overnight vs. včerejší close bez dat"
    # Scénář nad verdiktem bez živé ceny nevznikne (čeká na úplný verdikt)
    scenarios = ScenariosRepository(writer.repository._engine)
    scenarios.ensure_schema()
    generator = ScenarioGenerator("ES", scenarios)
    asyncio.run(generator.on_minute(now, stale, _runtime(SESSION)))
    assert not scenarios.exists_auto("ES", SESSION)
    # Živá cena další minutu: úplný verdikt přepíše neúplný řádek
    live = _minute(writer, clock, now + dt.timedelta(minutes=1), 7590.0)
    assert live is not None and live.complete
    (row,) = _rows(writer)
    assert row["missing"] == []


class _HangingApi(FakeApi):
    """API, které neodpovídá (visící spojení) — čtení se nikdy nedokončí samo."""

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await asyncio.sleep(3600)


def test_visici_api_nezdrzi_minutovy_cyklus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strop čtení: minuta se nezapíše a cyklus pokračuje; uložený řádek zůstane."""
    monkeypatch.setattr("gexlens_engine.verdict_writer.VERDICT_READ_TIMEOUT_S", 0.05)
    clock = _Clock(OPEN)
    writer = _writer(tmp_path, _full_api(), clock)
    assert _minute(writer, clock, OPEN - dt.timedelta(minutes=3), 7590.0).complete
    (before,) = _rows(writer)
    writer.api = _HangingApi({})  # type: ignore[assignment]
    assert _minute(writer, clock, OPEN - dt.timedelta(minutes=2), 7640.0) is None
    (after,) = _rows(writer)
    assert after["updated_at"] == before["updated_at"]


def test_vikend_a_svatek_bez_zapisu_a_bez_cteni_api(tmp_path: Path) -> None:
    """#1309: sobota, neděle a svátek NYSE nemají US open — žádný řádek, žádný dotaz."""
    clock = _Clock(OPEN)
    api = _full_api()
    writer = _writer(tmp_path, api, clock)
    for day in (dt.date(2026, 9, 26), dt.date(2026, 9, 27), dt.date(2026, 11, 26)):
        for offset in (15, 5, 1):
            now = us_open_ts(day) - dt.timedelta(minutes=offset)
            clock.now = now
            assert asyncio.run(writer.on_minute(now, 7590.0, _runtime(day))) is None
    assert _rows(writer) == [] and api.calls == []


def test_zimni_cas_okno_podle_us_openu(tmp_path: Path) -> None:
    """2. 11. 2026: USA už v zimním čase — open 14:30 UTC, okno 14:15–14:29 UTC."""
    monday = dt.date(2026, 11, 2)
    friday = dt.date(2026, 10, 30)
    clock = _Clock(us_open_ts(monday))
    writer = _writer(tmp_path, _full_api(monday, friday), clock)
    assert us_open_ts(monday) == dt.datetime(2026, 11, 2, 14, 30, tzinfo=dt.UTC)
    # 13:21 UTC: v létě by byla v minutovém okně, v zimě je před ním a mimo pětiminutový rytmus
    early = dt.datetime(2026, 11, 2, 13, 21, tzinfo=dt.UTC)
    clock.now = early
    assert asyncio.run(writer.on_minute(early, 7590.0, _runtime(monday))) is None
    inside = dt.datetime(2026, 11, 2, 14, 16, tzinfo=dt.UTC)  # mimo rytmus, ale v okně
    clock.now = inside
    result = asyncio.run(writer.on_minute(inside, 7590.0, _runtime(monday)))
    assert result is not None
    (row,) = _rows(writer)
    assert row["session_date"] == monday
