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


SUNDAY_OPEN = dt.datetime(2026, 10, 11, 22, 0, tzinfo=dt.UTC)  # 17:00 CT, otevření Globexu


@pytest.fixture
def weekend_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'meta.sqlite'}",
    )
    friday = [
        Bar(dt.datetime(2026, 10, 9, 19, 0, tzinfo=dt.UTC) + i * dt.timedelta(minutes=1),
            5000.0, 5000.5, 4999.5, 5000.0, 10.0)
        for i in range(120)
    ]  # fmt: skip
    sunday = [
        Bar(SUNDAY_OPEN + i * dt.timedelta(minutes=1), 5010.0, 5010.5, 5009.5, 5010.0, 10.0)
        for i in range(30)
    ]
    for symbol in ("ES", "NQ"):
        folder = settings.data_dir / "derived" / symbol / "bars"
        folder.mkdir(parents=True)
        for day, bars in (("2026-10-09", friday), ("2026-10-11", sunday)):
            table = pa.table(
                {
                    "ts_min": pa.array([b.ts for b in bars], pa.timestamp("us", tz="UTC")),
                    "open": [b.open for b in bars],
                    "high": [b.high for b in bars],
                    "low": [b.low for b in bars],
                    "close": [b.close for b in bars],
                    "volume": [b.volume for b in bars],
                }
            )
            pq.write_table(table, folder / f"{day}.parquet")
    app = create_app(settings)
    engine = MetaRepository(settings).engine()
    ensure_sentiment_schema(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(news_events),
            [
                _event(1, SUNDAY_OPEN, SUNDAY_OPEN + dt.timedelta(seconds=2), source="alpaca",
                       tier=2, category="GEOPOLITICS", importance=3,
                       title="Israel strikes targets in Iran"),
            ],
        )  # fmt: skip
    return TestClient(app)


def test_news_at_globex_open_takes_base_from_friday_close(weekend_client: TestClient) -> None:
    """Zpráva přesně v otevření: základ je páteční close jako v `ReactionJob`
    (poslední bar před zprávou až 5 dní zpět), dopad tedy nese gap přes víkend."""
    at = SUNDAY_OPEN + dt.timedelta(minutes=10)
    body = weekend_client.get("/news/breaking", params={"at": at.isoformat()}).json()
    (item,) = body["items"]
    es = item["impact"]["ES"]
    assert es["state"] == "fixed"
    assert es["ret_bp"] == pytest.approx((5010.0 - 5000.0) / 5000.0 * 10_000)


def test_rewritten_bars_recompute_fixed_impact(client: TestClient, tmp_path: Path) -> None:
    """Zafixovaný dopad se pamatuje podle obsahu barů — doplní-li engine nebo
    backfill jiné bary, karta nesmí ukázat zmrzlé číslo."""
    params = {"at": AT.isoformat()}
    before = client.get("/news/breaking", params=params).json()
    again = client.get("/news/breaking", params=params).json()
    fed = next(item for item in before["items"] if item["id"] == 1)
    assert next(item for item in again["items"] if item["id"] == 1)["impact"] == fed["impact"]

    path = tmp_path / "data" / "derived" / "ES" / "bars" / "2026-10-08.parquet"
    table = pq.read_table(path)
    doubled = [
        value * 2 - 5000.0 if ts >= FED_TS.replace(second=0) else value
        for ts, value in zip(
            table.column("ts_min").to_pylist(), table.column("close").to_pylist(), strict=True
        )
    ]
    pq.write_table(table.set_column(4, "close", pa.array(doubled)), path)
    after = client.get("/news/breaking", params=params).json()
    changed = next(item for item in after["items"] if item["id"] == 1)["impact"]
    assert changed["ES"]["ret_bp"] != fed["impact"]["ES"]["ret_bp"]
    assert changed["NQ"] == fed["impact"]["NQ"]  # NQ partice se nezměnila
