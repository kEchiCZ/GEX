"""Karta Breaking news `GET /news/breaking` (E-6.28b, ADR-0059 bod 4 a 8)."""

import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from gexlens_api.main import create_app
from gexlens_api.meta_repo import MetaRepository
from gexlens_engine.config import Settings
from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    news_event_sources,
    news_events,
)
from gexlens_news.reactions import Bar, compute_reactions

# Čtvrtek, trh otevřený; zpráva uprostřed minuty
FED_TS = dt.datetime(2026, 10, 8, 14, 30, 20, tzinfo=dt.UTC)
ARTICLE_TS = dt.datetime(2026, 10, 8, 14, 33, 0, tzinfo=dt.UTC)
AT = dt.datetime(2026, 10, 8, 14, 37, 30, tzinfo=dt.UTC)
SATURDAY_TS = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.UTC)


def _bars(first: dt.datetime, last: dt.datetime, *, jump_from: dt.datetime) -> list[Bar]:
    bars: list[Bar] = []
    ts = first
    while ts <= last:
        offset = int((ts - jump_from) / dt.timedelta(minutes=1))
        price = 5000.0 + (0.25 if offset % 2 else -0.25) if offset < 0 else 5000.0 + offset + 1
        bars.append(Bar(ts, price, price + 0.5, price - 0.5, price, 10.0))
        ts += dt.timedelta(minutes=1)
    return bars


BARS = _bars(
    dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.UTC),
    dt.datetime(2026, 10, 8, 15, 0, tzinfo=dt.UTC),
    jump_from=FED_TS.replace(second=0),
)


def _write_bars(data_dir: Path) -> None:
    for symbol in ("ES", "NQ"):
        folder = data_dir / "derived" / symbol / "bars"
        folder.mkdir(parents=True)
        table = pa.table(
            {
                "ts_min": pa.array([b.ts for b in BARS], pa.timestamp("us", tz="UTC")),
                "open": [b.open for b in BARS],
                "high": [b.high for b in BARS],
                "low": [b.low for b in BARS],
                "close": [b.close for b in BARS],
                "volume": [b.volume for b in BARS],
            }
        )
        pq.write_table(table, folder / "2026-10-08.parquet")


def _event(
    event_id: int,
    ts_event: dt.datetime,
    ts_ingested: dt.datetime,
    *,
    source: str,
    tier: int | None,
    category: str,
    importance: int,
    title: str,
) -> dict[str, object]:
    return {
        "id": event_id,
        "ts_event": ts_event,
        "ts_ingested": ts_ingested,
        "source": source,
        "kind": "headline",
        "title": title,
        "category": category,
        "importance": importance,
        "content_tier": tier,
        "symbols": [],
        "market_closed": False,
        "dedup_hash": f"h{event_id}",
        "raw": {},
    }


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}",
    )
    _write_bars(settings.data_dir)
    app = create_app(settings)
    engine = MetaRepository(settings).engine()
    ensure_sentiment_schema(engine)
    second = dt.timedelta(seconds=1)
    with engine.begin() as conn:
        conn.execute(
            insert(news_events),
            [
                _event(1, FED_TS, FED_TS + second, source="alpaca", tier=2, category="FED",
                       importance=3, title="Fed's Powell says rate cuts are not imminent"),
                # Starý článek Finnhubu: 80 min po publikaci → varianta C ho vyřadí
                _event(2, FED_TS - dt.timedelta(hours=1), FED_TS + dt.timedelta(minutes=20),
                       source="finnhub", tier=3, category="FED", importance=3,
                       title="Fed officials signal patience on rates"),
                # Čerstvý článek, kontaminuje okno zprávy 1 (jiná kategorie, K1)
                _event(3, ARTICLE_TS, ARTICLE_TS + 30 * second, source="rss_news", tier=3,
                       category="GEOPOLITICS", importance=2,
                       title="Iran seizes tanker in Strait of Hormuz"),
                # Přijatá až po `AT` — v čase AT neexistuje
                _event(4, AT - dt.timedelta(minutes=1), AT + dt.timedelta(minutes=1),
                       source="alpaca", tier=2, category="FED", importance=3,
                       title="Fed's Waller backs a December cut"),
                # Nevýznamná
                _event(5, FED_TS, FED_TS + second, source="alpaca", tier=2, category="OTHER",
                       importance=1, title="Analyst raises price target"),
                _event(6, SATURDAY_TS, SATURDAY_TS + second, source="alpaca", tier=2,
                       category="GEOPOLITICS", importance=3,
                       title="Israel strikes targets in Iran"),
            ],
        )  # fmt: skip
        # Kopie článku 3 z headline feedu, viditelná až po AT
        conn.execute(
            insert(news_event_sources).values(
                event_id=3,
                source="alpaca",
                source_uid="x",
                content_tier=2,
                published_at=ARTICLE_TS,
                fetched_at=AT + dt.timedelta(minutes=2),
            )
        )
    return TestClient(app)


def test_card_selects_breaking_news_point_in_time(client: TestClient) -> None:
    body = client.get("/news/breaking", params={"at": AT.isoformat()}).json()
    assert body["live"] is False and body["market_closed"] is False
    assert [item["id"] for item in body["items"]] == [3, 1]

    article, fed = body["items"]
    assert article["confirmed"] is False and article["effective_tier"] == 3
    assert article["group"] == "geopolitics" and article["theme"] == "iran"
    assert [s["source"] for s in article["sources"]] == ["rss_news"]  # kopie ještě není
    assert article["impact"]["ES"]["state"] == "running"
    assert article["impact"]["ES"]["elapsed_min"] == 4

    assert fed["confirmed"] is True and fed["is_key"] is True
    assert fed["group"] == "central_banks" and fed["theme"] == "fed"
    assert fed["sources"][0]["delay_s"] == 1.0
    es = fed["impact"]["ES"]
    assert es["state"] == "fixed" and es["contaminated"] is True  # článek 3 v okně
    # Replay bere jen uzavřené minuty, ty pokrývají celé 5min okno
    visible = [bar for bar in BARS if bar.ts + dt.timedelta(minutes=1) <= AT]
    (expected,) = compute_reactions(FED_TS, visible, windows=(5,))
    assert es["ret_bp"] == pytest.approx(expected.ret_bp)
    assert es["excursion_bp"] is not None and es["excursion_direction"] == 1
    assert fed["impact"]["NQ"]["ret_bp"] == pytest.approx(expected.ret_bp)


def test_copy_confirms_article_once_visible(client: TestClient) -> None:
    later = AT + dt.timedelta(minutes=3)
    body = client.get("/news/breaking", params={"at": later.isoformat()}).json()
    article = next(item for item in body["items"] if item["id"] == 3)
    assert article["confirmed"] is True and article["effective_tier"] == 2
    assert [s["source"] for s in article["sources"]] == ["rss_news", "alpaca"]
    assert article["sources"][1]["delay_s"] == 390.0  # 14:39:30 − 14:33:00
    assert article["impact"]["ES"]["state"] == "fixed"
    assert 4 in {item["id"] for item in body["items"]}  # mezitím přijatá


def test_news_during_closed_market_has_no_impact(client: TestClient) -> None:
    at = SATURDAY_TS + dt.timedelta(minutes=10)
    body = client.get("/news/breaking", params={"at": at.isoformat(), "hours": 1}).json()
    assert body["market_closed"] is True
    (item,) = body["items"]
    assert item["id"] == 6
    assert item["impact"]["ES"] == {
        "state": "closed",
        "elapsed_min": 10,
        "ret_bp": None,
        "range_bp": None,
        "excursion_bp": None,
        "excursion_direction": None,
        "contaminated": False,
    }


def test_at_without_timezone_is_rejected(client: TestClient) -> None:
    response = client.get("/news/breaking", params={"at": "2026-10-08T14:37:30"})
    assert response.status_code == 422
