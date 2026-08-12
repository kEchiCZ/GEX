"""Integrační test SentIndex jobu (#283): řada, denní svíčka, idempotence."""

import datetime as dt
from pathlib import Path

import pyarrow.parquet as pq
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import ensure_sentiment_schema, news_events, sentiment_daily
from gexlens_news.sentindex_job import SentIndexJob

NOW = dt.datetime(2026, 7, 28, 6, 0, tzinfo=dt.UTC)


def add_event(
    engine: Engine,
    ts: dt.datetime,
    *,
    category: str,
    score: float | None,
    importance: int = 3,
    key: str,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(news_events).values(
                ts_event=ts,
                ts_ingested=ts,
                source="rss_news",
                kind="headline",
                title=key,
                category=category,
                importance=importance,
                sentiment_dir=None if score is None else (1 if score > 0 else -1),
                sentiment_score=score,
                sentiment_source="rule",
                symbols=[],
                market_closed=False,
                dedup_hash=key,
                raw={},
            )
        )


def make_job(tmp_path: Path) -> tuple[Engine, SentIndexJob]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine, SentIndexJob(engine, tmp_path / "data")


def test_job_writes_series_and_daily_candle(tmp_path: Path) -> None:
    engine, job = make_job(tmp_path)
    # Večerní geopolitická zpráva z předchozího dne — musí doznívat do rána
    add_event(
        engine,
        dt.datetime(2026, 7, 27, 22, 0, tzinfo=dt.UTC),
        category="GEOPOLITICS",
        score=-0.8,
        key="vecerni",
    )
    add_event(engine, NOW - dt.timedelta(minutes=30), category="FED", score=0.4, key="ranni")

    points, topics = job.run(NOW)
    assert points == 361  # 00:00–06:00 po minutě

    # Per-symbol layout (ADR-0026): partice pro každý symbol z výčtu
    path = tmp_path / "data" / "derived" / "sentiment" / "ES" / "2026-07-28.parquet"
    assert path.exists()
    assert (tmp_path / "data" / "derived" / "sentiment" / "NQ" / "2026-07-28.parquet").exists()
    rows = pq.read_table(path).to_pylist()
    assert len(rows) == points
    # Open nese zbytek noční zprávy — celý smysl kontinuálního indexu
    assert rows[0]["value"] < 0

    with engine.connect() as conn:
        candles = conn.execute(select(sentiment_daily)).fetchall()
    by_symbol = {row.symbol: row for row in candles}
    assert set(by_symbol) == {"ES", "NQ"}
    candle = by_symbol["ES"]
    assert candle.open < 0
    assert candle.low <= candle.close <= candle.high

    assert {t.category for t in topics} == {"GEOPOLITICS", "FED"}
    assert all(not t.active for t in topics)  # po jedné zprávě se topic neaktivuje


def test_unclassified_events_do_not_enter_the_index(tmp_path: Path) -> None:
    engine, job = make_job(tmp_path)
    add_event(engine, NOW - dt.timedelta(minutes=5), category="TECH", score=None, key="bez_skore")

    points, topics = job.run(NOW)
    assert topics == []
    rows = pq.read_table(
        tmp_path / "data" / "derived" / "sentiment" / "ES" / "2026-07-28.parquet"
    ).to_pylist()
    assert all(row["value"] == 0.0 for row in rows)
    assert points > 0


def test_rerun_overwrites_instead_of_duplicating(tmp_path: Path) -> None:
    """Řada se počítá celá znovu — opakovaný běh nesmí zdvojit svíčku."""
    engine, job = make_job(tmp_path)
    add_event(engine, NOW - dt.timedelta(minutes=10), category="FED", score=0.5, key="a")

    job.run(NOW)
    job.run(NOW + dt.timedelta(minutes=5))

    with engine.connect() as conn:
        candles = conn.execute(select(sentiment_daily)).fetchall()
    assert len(candles) == 2  # upsert per symbol (ES + NQ), ne insert
    files = list((tmp_path / "data" / "derived" / "sentiment" / "ES").glob("*.parquet"))
    assert len(files) == 1
