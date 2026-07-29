"""Testy REST endpointů SentimentLensu (#285) a WS kanálů (#286)."""

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
    news_events,
    sentiment_daily,
)

# Endpoint /news/upcoming porovnává s reálným časem, takže fixture musí být
# relativní — pevné datum by test po pár hodinách přestalo platit
NOW = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}",
    )
    app = create_app(settings)
    engine = MetaRepository(settings).engine()
    ensure_sentiment_schema(engine)

    with engine.begin() as conn:
        conn.execute(
            insert(news_events),
            [
                {
                    "ts_event": NOW - dt.timedelta(hours=1),
                    "ts_ingested": NOW,
                    "source": "rss_news",
                    "kind": "headline",
                    "title": "Fed holds rates",
                    "category": "FED",
                    "importance": 3,
                    "sentiment_dir": 1,
                    "sentiment_score": 0.4,
                    "sentiment_source": "rule",
                    "symbols": [],
                    "market_closed": False,
                    "dedup_hash": "a",
                    "raw": {},
                },
                {
                    "ts_event": NOW + dt.timedelta(minutes=30),
                    "ts_ingested": NOW,
                    "source": "forexfactory",
                    "kind": "scheduled",
                    "title": "USD CPI m/m",
                    "category": "MACRO_INFLATION",
                    "importance": 3,
                    "sentiment_dir": None,
                    "sentiment_score": None,
                    "sentiment_source": None,
                    "symbols": [],
                    "market_closed": False,
                    "dedup_hash": "b",
                    "raw": {},
                },
            ],
        )
        conn.execute(
            insert(sentiment_daily).values(
                date=NOW.date(),
                symbol="ES",
                open=-0.5,
                high=1.1,
                low=-1.7,
                close=0.5,
                update_time=NOW,
            )
        )

    series_dir = settings.data_dir / "derived" / "sentiment"
    series_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(
            [{"ts_min": NOW, "value": 0.5}],
            schema=pa.schema([("ts_min", pa.timestamp("us", tz="UTC")), ("value", pa.float64())]),
        ),
        series_dir / f"{NOW.date().isoformat()}.parquet",
    )
    return TestClient(app)


def test_news_feed_filters(client: TestClient) -> None:
    assert len(client.get("/news").json()["news"]) == 2
    assert len(client.get("/news", params={"kind": "headline"}).json()["news"]) == 1
    assert len(client.get("/news", params={"category": "FED"}).json()["news"]) == 1
    # importance je dolní mez, ne přesná shoda
    assert len(client.get("/news", params={"importance": 3}).json()["news"]) == 2


def test_upcoming_returns_only_future_scheduled(client: TestClient) -> None:
    upcoming = client.get("/news/upcoming").json()["upcoming"]
    assert [u["title"] for u in upcoming] == ["USD CPI m/m"]


def test_static_routes_are_not_swallowed_by_path_param(client: TestClient) -> None:
    """`/news/upcoming` a `/news/stats` nesmí spadnout do `/news/{event_id}`."""
    assert client.get("/news/upcoming").status_code == 200
    assert client.get("/news/stats").status_code == 200
    assert client.get("/sentiment/state").status_code == 200
    assert client.get("/sentiment/daily").status_code == 200
    assert client.get("/sentiment/topics").status_code == 200


def test_news_detail_includes_classification_history(client: TestClient) -> None:
    event_id = client.get("/news").json()["news"][0]["id"]
    detail = client.get(f"/news/{event_id}").json()
    assert detail["event"]["id"] == event_id
    # Klíče drží tvar i když jsou zatím prázdné
    assert detail["reactions"] == []
    assert detail["classifications"] == []
    assert detail["predictions"] == []
    assert client.get("/news/999999").status_code == 404


def test_sentiment_index_reads_series_and_survives_missing_day(client: TestClient) -> None:
    today = client.get("/sentiment/index/ES", params={"date": NOW.date().isoformat()}).json()
    assert len(today["series"]) == 1
    assert today["series"][0]["value"] == pytest.approx(0.5)
    # Den bez partice vrací prázdnou řadu, ne chybu
    missing = client.get("/sentiment/index/ES", params={"date": "2020-01-01"}).json()
    assert missing["series"] == []


def test_daily_candles_and_state(client: TestClient) -> None:
    daily = client.get("/sentiment/daily").json()["daily"]
    assert len(daily) == 1
    assert daily[0]["open"] == pytest.approx(-0.5)

    state = client.get("/sentiment/state").json()
    # Jediný den v řadě → MA okna nejsou plná → Neutral (#292, SPEC 5.6)
    assert state["state"] == "Neutral"
    assert state["unconfirmed"] is False
    assert state["last_close"] == pytest.approx(0.5)
    assert state["current_wave"] is None


def test_topics_computed_from_live_events(client: TestClient) -> None:
    topics = client.get("/sentiment/topics").json()["topics"]
    assert [t["category"] for t in topics] == ["FED"]
    assert not topics[0]["active"]  # jedna zpráva topic neaktivuje
    assert client.get("/sentiment/topics", params={"active": 1}).json()["topics"] == []


def test_later_milestone_endpoints_hold_shape(client: TestClient) -> None:
    """Signály, review a statistiky existují už teď — N7/N8 mění data, ne API."""
    assert client.get("/signals").json() == {"signals": []}
    assert client.get("/review").json() == {"review": []}
    assert client.get("/stats/waves").json() == {"waves": []}
    assert client.get("/stats/trackrecord").json() == {"track_record": []}
    assert client.get("/sentiment/crowd").json() == {"crowd": []}


def test_summary_counts(client: TestClient) -> None:
    summary = client.get("/sentiment/summary").json()
    assert summary == {"events": 2, "reactions": 0, "model_buckets": 0}


def test_ws_channels_accept_news_subscriptions(client: TestClient) -> None:
    """#286: kanály jsou generické, stačí je odebírat a publikovat."""
    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"action": "subscribe", "channels": ["news", "sentiment.ES", "news.upcoming"]})
        ack = ws.receive_json()
        assert ack["type"] == "ack"
        assert set(ack["channels"]) == {"news", "sentiment.ES", "news.upcoming"}

        client.post(
            "/internal/publish",
            json={"channel": "sentiment.ES", "data": {"value": 0.42, "topics": []}},
        )
        message = ws.receive_json()
        assert message["channel"] == "sentiment.ES"
        assert message["data"]["value"] == pytest.approx(0.42)


def test_numeric_columns_are_json_numbers_not_strings(client: TestClient) -> None:
    """PG vrací Numeric jako Decimal — serializace na řetězec shodí frontend.

    UI nad hodnotou volá `toFixed`, takže string znamená pád celé aplikace,
    ne jen špatné zobrazení.
    """
    row = next(r for r in client.get("/news").json()["news"] if r["sentiment_score"] is not None)
    assert isinstance(row["sentiment_score"], (int, float))
    assert not isinstance(row["sentiment_score"], str)


def test_review_correction_creates_manual_version(client: TestClient) -> None:
    """#293: korekce → nová verze source=manual + denormalizace + resolved."""
    event_id = client.get("/news").json()["news"][0]["id"]

    # Prázdná korekce se odmítá
    assert client.post(f"/review/{event_id}", json={}).status_code == 422
    # Neznámá kategorie se odmítá
    assert client.post(f"/review/{event_id}", json={"category": "HACKED"}).status_code == 422
    assert client.post("/review/999999", json={"direction": 1}).status_code == 404

    response = client.post(f"/review/{event_id}", json={"direction": -1, "category": "GEOPOLITICS"})
    assert response.status_code == 200
    body = response.json()
    assert body["direction"] == -1
    assert body["category"] == "GEOPOLITICS"

    # Nová verze je v historii klasifikací (S11 — append, ne přepis)
    detail = client.get(f"/news/{event_id}").json()
    versions = detail["classifications"]
    assert versions[-1]["source"] == "manual"
    assert versions[-1]["direction"] == -1
    # Denormalizace na eventu se propsala
    assert detail["event"]["sentiment_source"] == "manual"
    assert detail["event"]["category"] == "GEOPOLITICS"
