"""Golden testy historického FF kalendáře a surprise_z (#277, ADR-0018)."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import ensure_sentiment_schema, news_events
from gexlens_news.collectors.forexfactory import ForexFactoryCollector
from gexlens_news.ffhistory import (
    FfActualRefreshJob,
    extract_days,
    mondays_back,
    normalize_entry,
    recompute_surprise_z,
    run_backfill,
    update_actuals,
    week_param,
)
from gexlens_news.model import RawItem
from gexlens_news.store import NewsWriter

NOW = dt.datetime(2026, 7, 29, 12, 0, tzinfo=dt.UTC)

# Reálný tvar vloženého objektu (zachyceno 29. 7. 2026, týden jul7.2025):
# JS obal s neuvozeným klíčem `days:`, vnitřek striktní JSON s \/ escapy
PAGE_TEMPLATE = """<!DOCTYPE html><html><head></head><body>
<script>if (typeof window.calendarComponentStates === 'undefined')
{{ window.calendarComponentStates = {{}} }}
window.calendarComponentStates[1] = {{
days: {days},
other: [1, 2]}};
</script></body></html>"""


def page(days_json: str) -> str:
    return PAGE_TEMPLATE.format(days=days_json)


CPI_ENTRY = {
    "id": 143921,
    "name": "CPI m/m",
    "currency": "USD",
    "dateline": 1751968800,  # 2025-07-08 10:00:00 UTC
    "impactName": "high",
    "forecast": "0.3%",
    "previous": "0.2%",
    "actual": "0.4%",
}


# ── Parsování stránky ──────────────────────────────────────────────


def test_extract_days_from_js_wrapper() -> None:
    html = page('[{"date":"Mon <span>Jul 7<\\/span>","dateline":1751839200,"events":[{"id":1}]}]')
    days = extract_days(html)
    assert len(days) == 1
    assert days[0]["events"] == [{"id": 1}]


def test_extract_days_survives_format_change() -> None:
    assert extract_days("<html>úplně jiná stránka</html>") == []
    assert extract_days(page('[{"broken":')) == []


def test_week_param_and_mondays() -> None:
    assert week_param(dt.date(2025, 7, 7)) == "jul7.2025"
    assert week_param(dt.date(2024, 1, 1)) == "jan1.2024"
    mondays = mondays_back(3, today=dt.date(2026, 7, 29))  # středa
    assert mondays == [dt.date(2026, 7, 6), dt.date(2026, 7, 13), dt.date(2026, 7, 20)]
    # Aktuální týden se nestahuje — patří živému collectoru a refresh jobu
    assert dt.date(2026, 7, 27) not in mondays


# ── Normalizace ────────────────────────────────────────────────────


def test_normalize_entry_matches_live_collector_identity() -> None:
    """Titulek, source_uid i dedup_hash musí sedět s živým collectorem —
    jinak by se backfill s živě sebraným eventem nesešel na dedup_hash."""
    history = normalize_entry(CPI_ENTRY, fetched_at=NOW)
    assert history is not None

    live = ForexFactoryCollector(fetcher=None).normalize(  # type: ignore[arg-type]
        RawItem(
            source="forexfactory",
            payload={
                "title": "CPI m/m",
                "country": "USD",
                # Týž okamžik v US Eastern (EDT −4): 10:00 UTC = 06:00-04:00
                "date": "2025-07-08T06:00:00-04:00",
                "impact": "High",
                "forecast": "0.3%",
                "previous": "0.2%",
            },
            fetched_at=NOW,
        )
    )
    assert live is not None
    assert history.title == live.title == "USD CPI m/m"
    assert history.ts_event == live.ts_event
    assert history.source_uid == live.source_uid
    assert history.dedup_hash == live.dedup_hash
    # Historie navíc nese actual, který widget feed nemá (ADR-0013)
    assert history.actual == pytest.approx(0.4)
    assert history.importance == 3
    assert history.symbols == ["ES", "NQ"]


def test_normalize_entry_rejects_junk() -> None:
    assert normalize_entry({"name": "", "dateline": 123}, fetched_at=NOW) is None
    assert normalize_entry({"name": "X", "dateline": None}, fetched_at=NOW) is None
    assert normalize_entry({"name": "X", "dateline": "zítra"}, fetched_at=NOW) is None


# ── surprise_z ─────────────────────────────────────────────────────


def make_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine


def seed_series(engine: Engine, values: list[tuple[float, float]], *, title: str) -> None:
    """Řada scheduled eventů (forecast, actual) po měsících."""
    writer = NewsWriter(engine)
    events = []
    for index, (forecast, actual) in enumerate(values):
        ts = dt.datetime(2025, 1, 10, 13, 30, tzinfo=dt.UTC) + dt.timedelta(days=30 * index)
        entry = dict(
            CPI_ENTRY,
            name=title,
            dateline=int(ts.timestamp()),
            forecast=str(forecast),
            actual=str(actual),
        )
        event = normalize_entry(entry, fetched_at=NOW)
        assert event is not None
        events.append(event)
    writer.write(events)


def test_surprise_z_golden(tmp_path: Path) -> None:
    """Golden výpočet ze SPEC: z = (actual − forecast) / výběrová σ překvapení.

    Překvapení: [0.1, -0.1, 0.1, -0.1, 0.1, 0.3] → mean 0.0666…,
    σ = stdev = 0.1505545305... → poslední z = 0.3/0.1506 ≈ 1.9926.
    """
    engine = make_db(tmp_path)
    values = [(0.2, 0.3), (0.2, 0.1), (0.3, 0.4), (0.3, 0.2), (0.1, 0.2), (0.2, 0.5)]
    seed_series(engine, values, title="CPI m/m")

    assert recompute_surprise_z(engine) == 6

    with engine.connect() as conn:
        stored = conn.execute(
            select(news_events.c.surprise_z).order_by(news_events.c.ts_event)
        ).fetchall()
    z_values = [float(row.surprise_z) for row in stored]
    assert z_values[-1] == pytest.approx(0.3 / 0.15055453054, rel=1e-6)
    assert z_values[0] == pytest.approx(0.1 / 0.15055453054, rel=1e-6)
    # Druhý běh nic nemění (idempotence)
    assert recompute_surprise_z(engine) == 0


def test_surprise_z_needs_minimum_samples(tmp_path: Path) -> None:
    """σ z pár měření je šum — krátká řada z nedostane."""
    engine = make_db(tmp_path)
    seed_series(engine, [(0.2, 0.3), (0.2, 0.1)], title="Obscure Index")
    assert recompute_surprise_z(engine) == 0


# ── Backfill a refresh ─────────────────────────────────────────────


def days_json_for(entry: dict) -> str:
    import json

    return json.dumps([{"date": "Mon", "dateline": entry["dateline"], "events": [entry]}])


def test_backfill_writes_and_is_idempotent(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    fetched: list[str] = []

    def fake_fetch(week: str) -> str:
        fetched.append(week)
        return page(days_json_for(CPI_ENTRY))

    stats = run_backfill(
        engine, weeks=2, today=dt.date(2026, 7, 29), fetch=fake_fetch, sleep=lambda _s: None
    )
    assert stats.weeks_fetched == 2
    assert fetched == ["jul13.2026", "jul20.2026"]
    # Obě stránky nesly týž event → zapsán jednou
    assert stats.written == 1

    again = run_backfill(
        engine, weeks=2, today=dt.date(2026, 7, 29), fetch=fake_fetch, sleep=lambda _s: None
    )
    assert again.written == 0  # idempotence


def test_backfill_survives_failed_week(tmp_path: Path) -> None:
    engine = make_db(tmp_path)

    def flaky_fetch(week: str) -> str:
        if week == "jul13.2026":
            raise OSError("timeout")
        return page(days_json_for(CPI_ENTRY))

    stats = run_backfill(
        engine, weeks=2, today=dt.date(2026, 7, 29), fetch=flaky_fetch, sleep=lambda _s: None
    )
    assert stats.weeks_fetched == 1
    assert stats.weeks_failed == 1
    assert stats.written == 1


def test_update_actuals_fills_only_missing(tmp_path: Path) -> None:
    """Event založený živým collectorem (bez actual) dostane hodnotu updatem;
    už vyplněný actual se nepřepisuje (revize nemění, na čem stavěly reakce)."""
    engine = make_db(tmp_path)
    live_like = normalize_entry(dict(CPI_ENTRY, actual=""), fetched_at=NOW)
    assert live_like is not None and live_like.actual is None
    NewsWriter(engine).write([live_like])

    history = normalize_entry(CPI_ENTRY, fetched_at=NOW)
    assert history is not None
    assert update_actuals(engine, [history]) == 1

    revised = normalize_entry(dict(CPI_ENTRY, actual="9.9%"), fetched_at=NOW)
    assert revised is not None
    assert update_actuals(engine, [revised]) == 0  # už vyplněné se nemění

    with engine.connect() as conn:
        row = conn.execute(select(news_events.c.actual)).one()
    assert float(row.actual) == pytest.approx(0.4)


def test_refresh_job_updates_past_events_only(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    past = dict(CPI_ENTRY, dateline=int((NOW - dt.timedelta(hours=2)).timestamp()))
    future = dict(
        CPI_ENTRY,
        name="PPI m/m",
        dateline=int((NOW + dt.timedelta(hours=2)).timestamp()),
    )
    # Živě založené eventy bez actual
    for entry in (past, future):
        event = normalize_entry(dict(entry, actual=""), fetched_at=NOW)
        assert event is not None
        NewsWriter(engine).write([event])

    import json

    def fake_fetch(_week: str) -> str:
        return page(
            json.dumps([{"date": "Wed", "dateline": past["dateline"], "events": [past, future]}])
        )

    job = FfActualRefreshJob(engine, fetch=fake_fetch, interval_s=3600)
    assert job.due(NOW)
    assert job.run(NOW) == 1  # jen proběhlý event

    with engine.connect() as conn:
        rows = conn.execute(select(news_events.c.title, news_events.c.actual)).fetchall()
    by_title = {row.title: row.actual for row in rows}
    assert float(by_title["USD CPI m/m"]) == pytest.approx(0.4)
    assert by_title["USD PPI m/m"] is None
    # Interval guard: hned po běhu není due
    assert not job.due(NOW + dt.timedelta(minutes=30))
    assert job.due(NOW + dt.timedelta(hours=1))
