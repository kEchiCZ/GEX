"""Golden testy Tier C crowd collectorů (#290, ADR-0014)."""

import datetime as dt
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import crowd_sentiment, ensure_sentiment_schema
from gexlens_news.crowd import (
    CnnFearGreedCollector,
    CrowdPoint,
    CrowdRunner,
    CrowdWriter,
    PcrCollector,
    compute_pcr,
    parse_cnn_payload,
    parse_reddit_listing,
)

NOW = dt.datetime(2026, 7, 29, 14, 0, tzinfo=dt.UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "cnn_feargreed.json"


def load_fixture() -> dict[str, object]:
    # utf-8-sig: zachycený payload nese BOM
    payload: dict[str, object] = json.loads(FIXTURE.read_text(encoding="utf-8-sig"))
    return payload


# ── CNN Fear & Greed ───────────────────────────────────────────────


def test_cnn_parse_golden_fixture() -> None:
    """Fixture z ADR-0014: aktuální score + historie hlavního i sub-indexů."""
    points = parse_cnn_payload(load_fixture())

    current = next(p for p in points if p.metric == "score" and p.raw == {"rating": "fear"})
    assert current.value == pytest.approx(39.4285714285714)
    assert current.ts == dt.datetime(2026, 7, 27, 12, 39, 1, tzinfo=dt.UTC)
    assert current.source == "cnn_fg"

    # Historie hlavního score: epoch ms → UTC půlnoc obchodního dne
    historical = [p for p in points if p.metric == "score" and p is not current]
    assert historical
    assert historical[0].ts == dt.datetime(2025, 7, 28, tzinfo=dt.UTC)
    assert historical[0].value == pytest.approx(73.8)

    # Sub-indexy jako vlastní metriky (put_call_options, vix, …)
    metrics = {p.metric for p in points}
    assert "put_call_options" in metrics
    assert "market_volatility_vix_50" in metrics
    # Všechny metriky se vejdou do sloupce (String(32))
    assert all(len(p.metric) <= 32 for p in points)


def test_cnn_parse_survives_garbage() -> None:
    assert parse_cnn_payload({}) == []
    assert parse_cnn_payload({"fear_and_greed": "nesmysl", "x": {"data": "taky"}}) == []
    # Nečitelný bod se přeskočí, zbytek projde
    payload = {
        "fear_and_greed_historical": {
            "data": [{"x": "špatně", "y": 1}, {"x": 1753660800000.0, "y": 50.0}]
        }
    }
    points = parse_cnn_payload(payload)
    assert len(points) == 1
    assert points[0].value == 50.0


def test_cnn_collector_uses_injected_fetch() -> None:
    collector = CnnFearGreedCollector(interval_s=3600, fetch=load_fixture)
    points = collector.collect(NOW)
    assert len(points) > 4


# ── Reddit ─────────────────────────────────────────────────────────


def reddit_listing(scores: list[int]) -> dict[str, object]:
    return {
        "data": {
            "children": [
                {"data": {"title": f"Post {i}", "score": score}} for i, score in enumerate(scores)
            ]
        }
    }


def test_reddit_listing_average_and_titles() -> None:
    points = parse_reddit_listing(reddit_listing([100, 200, 600]), metric="wsb_hot_avg", now=NOW)
    assert len(points) == 1
    point = points[0]
    assert point.value == pytest.approx(300.0)
    assert point.source == "reddit"
    assert point.ts == NOW.replace(second=0, microsecond=0)
    assert point.raw is not None
    assert point.raw["posts"] == 3
    # Top titulky seřazené dle skóre, omezené limitem
    assert point.raw["top"][0]["score"] == 600


def test_reddit_listing_garbage_is_empty() -> None:
    assert parse_reddit_listing({}, metric="wsb_hot_avg", now=NOW) == []
    assert parse_reddit_listing({"data": {"children": "x"}}, metric="m", now=NOW) == []


# ── PCR ────────────────────────────────────────────────────────────


def test_compute_pcr_golden() -> None:
    rows = [
        {"right": "C", "volume": 1000.0},
        {"right": "C", "volume": 500.0},
        {"right": "P", "volume": 1200.0},
        {"right": "P", "volume": 600.0},
    ]
    result = compute_pcr(rows)
    assert result is not None
    pcr, call_volume, put_volume = result
    assert pcr == pytest.approx(1.2)
    assert call_volume == 1500.0
    assert put_volume == 1800.0
    # Nulové call volume → poměr nespočitatelný, ne dělení nulou
    assert compute_pcr([{"right": "P", "volume": 5.0}]) is None


def write_snapshot(path: Path, minutes: list[tuple[dt.datetime, str, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "ts_min": pa.array([m[0] for m in minutes], pa.timestamp("us", tz="UTC")),
            "right": pa.array([m[1] for m in minutes]),
            "volume": pa.array([m[2] for m in minutes]),
        }
    )
    pq.write_table(table, path)


def test_pcr_collector_reads_last_minute_of_active_expiry(tmp_path: Path) -> None:
    earlier = NOW - dt.timedelta(minutes=5)
    write_snapshot(
        tmp_path / "snapshots" / "ES" / "20260729" / "2026-07-29.parquet",
        [
            (earlier, "C", 10.0),  # starší minuta se ignoruje
            (NOW, "C", 2000.0),
            (NOW, "P", 1500.0),
        ],
    )
    # Vzdálenější expirace s dnešní particí nesmí vyhrát nad aktivní
    write_snapshot(
        tmp_path / "snapshots" / "ES" / "20260730" / "2026-07-29.parquet",
        [(NOW, "C", 1.0), (NOW, "P", 99.0)],
    )
    # Prošlá expirace bez dnešní partice se ignoruje
    (tmp_path / "snapshots" / "ES" / "20260728").mkdir(parents=True)

    collector = PcrCollector(tmp_path, ["ES", "NQ"], interval_s=300)
    points = collector.collect(NOW)

    assert len(points) == 1  # NQ nemá data → bez bodu, bez chyby
    point = points[0]
    assert point.symbol == "ES"
    assert point.metric == "pcr_volume"
    assert point.value == pytest.approx(0.75)
    assert point.raw is not None
    assert point.raw["expiry"] == "20260729"


# ── Writer a runner ────────────────────────────────────────────────


def make_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine


def point(metric: str = "score", value: float = 50.0) -> CrowdPoint:
    return CrowdPoint(ts=NOW, source="cnn_fg", metric=metric, value=value)


def test_writer_is_idempotent_on_pk(tmp_path: Path) -> None:
    writer = CrowdWriter(make_db(tmp_path))
    assert writer.write([point(), point("put_call_options", 0.9)]) == 2
    # Tentýž bod znovu (hodinový refetch celé historie) → nic nového
    assert writer.write([point()]) == 0


class FlakyCollector:
    name = "flaky"
    interval_s = 60.0

    def __init__(self) -> None:
        self.calls = 0

    def collect(self, now: dt.datetime) -> list[CrowdPoint]:
        self.calls += 1
        raise OSError("endpoint down")


class SteadyCollector:
    name = "steady"
    interval_s = 60.0

    def __init__(self) -> None:
        self.calls = 0

    def collect(self, now: dt.datetime) -> list[CrowdPoint]:
        self.calls += 1
        return [point(value=float(self.calls))]


def test_runner_isolates_failures_and_respects_intervals(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    flaky, steady = FlakyCollector(), SteadyCollector()
    runner = CrowdRunner([flaky, steady], CrowdWriter(engine))

    # Pád jednoho zdroje nezastaví druhý
    assert runner.run_due(NOW) == 1
    assert steady.calls == 1
    # Před uplynutím intervalu se zdroj nespouští
    runner.run_due(NOW + dt.timedelta(seconds=30))
    assert steady.calls == 1
    # Po intervalu ano
    runner.run_due(NOW + dt.timedelta(seconds=61))
    assert steady.calls == 2
    assert flaky.calls == 2

    with engine.connect() as conn:
        stored = conn.execute(select(crowd_sentiment.c.value)).fetchall()
    assert len(stored) == 1  # steady vrací týž PK (stejné NOW ts) → dedup
