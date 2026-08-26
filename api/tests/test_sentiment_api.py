"""Testy REST endpointů SentimentLensu (#285) a WS kanálů (#286)."""

import datetime as dt
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi import FastAPI
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

INTERNAL_TOKEN = "test-token"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Interní ingest je od #542 za tokenem; news-engine ho posílá stejně
    monkeypatch.setenv("GEXLENS_API_TOKEN", INTERNAL_TOKEN)
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
    schema = pa.schema([("ts_min", pa.timestamp("us", tz="UTC")), ("value", pa.float64())])
    # ES jako plochý legacy soubor (před ADR-0026) — fallback musí dál fungovat
    pq.write_table(
        pa.Table.from_pylist([{"ts_min": NOW, "value": 0.5}], schema=schema),
        series_dir / f"{NOW.date().isoformat()}.parquet",
    )
    # NQ v per-symbol layoutu (ADR-0026)
    (series_dir / "NQ").mkdir(exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([{"ts_min": NOW, "value": 0.7}], schema=schema),
        series_dir / "NQ" / f"{NOW.date().isoformat()}.parquet",
    )
    return TestClient(app, headers={"X-GEXLens-Token": INTERNAL_TOKEN})


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


def test_sentiment_index_per_symbol_layout(client: TestClient) -> None:
    """ADR-0026: NQ čte vlastní partici; legacy plochý soubor patří jen ES."""
    nq = client.get("/sentiment/index/NQ", params={"date": NOW.date().isoformat()}).json()
    assert len(nq["series"]) == 1
    assert nq["series"][0]["value"] == pytest.approx(0.7)
    # NQ bez partice NEpadá na ES legacy soubor — cizí řada je horší než žádná
    missing = client.get("/sentiment/index/NQ", params={"date": "2020-01-01"}).json()
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


def test_news_feed_carries_topic_value(client: TestClient) -> None:
    """#656 bod 5: karta nese index tématu k okamžiku zprávy."""
    rows = client.get("/news").json()["news"]
    by_title = {row["title"]: row for row in rows}
    # Jediná zpráva tématu: index v čase zprávy = její vlastní skóre (exp(0))
    assert by_title["Fed holds rates"]["topic_value"] == pytest.approx(0.4)
    # CPI nemá skóre ani žádnou skórovanou zprávu v kategorii → None
    assert by_title["USD CPI m/m"]["topic_value"] is None


def test_news_feed_scheduled_direction(client: TestClient) -> None:
    """#462 A: směr scheduled eventu z konvence řady (CPI −, payrolls +)."""
    from sqlalchemy import update

    app = cast(FastAPI, client.app)
    engine = app.state.meta_repository.engine()
    # CPI vyšlo níž než konsensus → surprise_z záporné, konvence CPI −1 → risk-on (+1)
    with engine.begin() as conn:
        conn.execute(
            update(news_events)
            .where(news_events.c.title == "USD CPI m/m")
            .values(actual=2.7, surprise_z=-1.4)
        )
    rows = client.get("/news").json()["news"]
    by_title = {row["title"]: row for row in rows}
    assert by_title["USD CPI m/m"]["surprise_direction"] == 1
    # Headline směr nedostává (klíč úplně chybí — jiný stav než None u scheduled)
    assert "surprise_direction" not in by_title["Fed holds rates"]


def test_topics_series_and_shares(client: TestClient) -> None:
    """#566 fáze 1+2: řada per téma + rozpad příspěvků za období."""
    payload = client.get("/sentiment/topics/series", params={"days": 1}).json()
    assert payload["step_minutes"] == 15
    topics = payload["topics"]
    # Jen FED má skóre (CPI je scheduled bez sentimentu) a nese celý podíl
    assert [t["category"] for t in topics] == ["FED"]
    fed = topics[0]
    assert fed["events"] == 1
    assert fed["share"] == pytest.approx(1.0)
    assert len(fed["points"]) > 0
    # Poslední bod řady je kladný (zpráva před hodinou ještě nedozněla)
    assert fed["points"][-1]["value"] > 0


def test_topic_events_listing(client: TestClient) -> None:
    """#566 fáze 3: dohledatelnost — zprávy, které téma tvoří."""
    payload = client.get("/sentiment/topics/FED/events", params={"days": 7}).json()
    assert [e["title"] for e in payload["events"]] == ["Fed holds rates"]
    assert payload["events"][0]["sentiment_score"] == pytest.approx(0.4)
    assert client.get("/sentiment/topics/NESMYSL/events").status_code == 404


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


def test_news_latency_measures_source_delay(client: TestClient) -> None:
    """#358: medián/p90 per zdroj; scheduled a latence nad strop mimo percentily."""
    payload = client.get("/news/latency").json()
    assert payload["days"] == 7
    by_source = {row["source"]: row for row in payload["latency"]}
    # Scheduled event (forexfactory) se neměří vůbec
    assert "forexfactory" not in by_source
    rss = by_source["rss_news"]
    assert rss["n"] == 1
    assert rss["median_s"] == pytest.approx(3600)
    assert rss["p90_s"] == pytest.approx(3600)
    assert rss["n_over_cutoff"] == 0


def test_news_feed_measured_reactions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#656: feed nese naměřený dopad z news_reactions per symbol + kontaminaci."""
    from gexlens_engine.storage.sentiment import news_reactions

    monkeypatch.setenv("GEXLENS_API_TOKEN", INTERNAL_TOKEN)
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}",
    )
    app = create_app(settings)
    engine = MetaRepository(settings).engine()
    ensure_sentiment_schema(engine)

    with engine.begin() as conn:
        inserted = conn.execute(
            insert(news_events).values(
                ts_event=NOW - dt.timedelta(hours=1),
                ts_ingested=NOW,
                source="rss_news",
                kind="headline",
                title="CPI hot",
                category="INFLATION",
                importance=3,
                sentiment_dir=-1,
                sentiment_score=-0.4,
                sentiment_source="rule",
                symbols=[],
                market_closed=False,
                dedup_hash="reakce-1",
                raw={},
            )
        ).inserted_primary_key
        assert inserted is not None
        event_id = int(inserted[0])
        conn.execute(
            insert(news_reactions),
            [
                {
                    "event_id": event_id,
                    "symbol": "ES",
                    "window_min": 5,
                    "ret_bp": -16.5,
                    "range_bp": 20.0,
                    "contaminated": False,
                    "deferred": False,
                    "computed_at": NOW,
                },
                {
                    "event_id": event_id,
                    "symbol": "ES",
                    "window_min": 15,
                    "ret_bp": -12.0,
                    "range_bp": 25.0,
                    "contaminated": True,
                    "deferred": False,
                    "computed_at": NOW,
                },
                {
                    "event_id": event_id,
                    "symbol": "NQ",
                    "window_min": 5,
                    "ret_bp": 99.0,
                    "range_bp": 30.0,
                    "contaminated": False,
                    "deferred": False,
                    "computed_at": NOW,
                },
            ],
        )

    api = TestClient(app)
    row = next(item for item in api.get("/news").json()["news"] if item["id"] == event_id)
    assert row["reactions_bp"] == {"5": -16.5, "15": -12.0}  # jen ES (default symbol)
    assert row["reaction_contaminated"] is True  # kterékoli okno kontaminované

    nq = next(
        item
        for item in api.get("/news", params={"symbol": "NQ"}).json()["news"]
        if item["id"] == event_id
    )
    assert nq["reactions_bp"] == {"5": 99.0}
    assert nq["reaction_contaminated"] is False
