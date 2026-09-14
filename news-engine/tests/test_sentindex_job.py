"""Integrační test SentIndex jobu (#283): řada, denní svíčka, idempotence."""

import datetime as dt
from pathlib import Path

import pyarrow.parquet as pq
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    news_events,
    news_weights,
    sentiment_daily,
)
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


def test_events_are_weighted_by_category_and_their_own_predictor(tmp_path: Path) -> None:
    """#1150: váha (kategorie, sentiment_source) per symbol; jiný predictor ani
    chybějící řádek event nenulují (neutrál 1.0)."""
    engine, job = make_job(tmp_path)
    ts = NOW - dt.timedelta(minutes=1)
    add_event(engine, ts, category="FED", score=0.4, key="rule-fed")
    with engine.begin() as conn:
        conn.execute(
            insert(news_weights),
            [
                {
                    "category": "FED",
                    "predictor": predictor,
                    "window_min": 5,
                    "symbol": symbol,
                    "n": 50,
                    "hit_rate": 0.5,
                    "hit_rate_lb": 0.4,
                    "weight": weight,
                    "computed_at": NOW,
                }
                for symbol, predictor, weight in (
                    ("ES", "rule", 0.5),
                    ("ES", "llm", 2.0),  # jiný predictor — na rule event nesmí sáhnout
                    ("NQ", "llm", 2.0),  # NQ pro rule žádný řádek → 1.0
                )
            ],
        )

    es = job.load_events(NOW, "ES")
    nq = job.load_events(NOW, "NQ")
    assert [e.score for e in es] == [0.4 * 0.5]
    assert [e.score for e in nq] == [0.4]


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


def test_refresh_z_kauzalni_sigma_a_minimum_historie(tmp_path: Path) -> None:
    """#640: σ z PŘEDCHOZÍCH ≤100 seancí (bez dneška), < 30 seancí → NULL."""
    engine, job = make_job(tmp_path)
    base = dt.date(2026, 1, 1)
    with engine.begin() as conn:
        for i in range(60):
            close = 0.1 if i % 2 == 0 else -0.1
            if i == 50:
                close = 5.0  # skoková výchylka — do VLASTNÍ σ vstoupit nesmí
            conn.execute(
                insert(sentiment_daily).values(
                    date=base + dt.timedelta(days=i),
                    symbol="ES",
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    update_time=NOW,
                )
            )

    updated = job.refresh_z("ES")

    assert updated > 0
    with engine.connect() as conn:
        rows = conn.execute(
            select(sentiment_daily.c.date, sentiment_daily.c.sigma, sentiment_daily.c.close_z)
            .where(sentiment_daily.c.symbol == "ES")
            .order_by(sentiment_daily.c.date)
        ).fetchall()
    # Prvních 30 dní: málo historie → NULL
    assert all(row.sigma is None for row in rows[:30])
    assert rows[35].sigma is not None and abs(rows[35].sigma - 0.1) < 1e-3
    # Kauzalita: den skoku má σ JEŠTĚ z klidné historie → obří z-score…
    assert rows[50].close_z is not None and rows[50].close_z > 40
    # …a σ následujícího dne už skok obsahuje (vyskočí nad klidových 0,1)
    assert rows[51].sigma is not None and rows[51].sigma > 0.5


def test_refresh_z_je_idempotentni(tmp_path: Path) -> None:
    engine, job = make_job(tmp_path)
    base = dt.date(2026, 1, 1)
    with engine.begin() as conn:
        for i in range(40):
            close = 0.1 if i % 2 == 0 else -0.1
            conn.execute(
                insert(sentiment_daily).values(
                    date=base + dt.timedelta(days=i),
                    symbol="ES",
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    update_time=NOW,
                )
            )

    first = job.refresh_z("ES")
    second = job.refresh_z("ES")

    assert first > 0
    # Druhý běh přepočítá nanejvýš „dnešek" — historie se nehoní dokola
    assert second <= 1
