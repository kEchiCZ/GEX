"""Testy schématu SentimentLensu (#269): založení, invarianty, dopřednost."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from gexlens_engine.storage.sentiment import (
    NEWS_CATEGORIES,
    REACTION_WINDOWS,
    crowd_sentiment,
    ensure_sentiment_schema,
    news_classifications,
    news_events,
    news_reactions,
    sentiment_metadata,
)

TS = dt.datetime(2026, 7, 28, 12, 30, tzinfo=dt.UTC)

# Tabulky pozdějších milestones se zakládají už teď (SPEC kap. 11) — dopředné
# migrace, aby N6–N8 nemusely couvat
EXPECTED_TABLES = {
    "news_events",
    "news_classifications",
    "news_reactions",
    "news_model_stats",
    "news_predictions",
    "news_prediction_outcomes",
    "sentiment_daily",
    "sentiment_waves",
    "crowd_sentiment",
    "signals",
    "review_queue",
    "track_record",
}


def make_engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'sentiment.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine


def test_schema_creates_all_tables_including_later_milestones(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
    # Idempotence — opakované volání nesmí spadnout (start enginu i API)
    ensure_sentiment_schema(engine)


def test_metadata_is_isolated_from_other_modules() -> None:
    """Vlastní MetaData: create_all SentimentLensu nesmí sahat na tabulky enginu."""
    assert set(sentiment_metadata.tables) >= EXPECTED_TABLES
    assert "setups" not in sentiment_metadata.tables
    assert "oi_eod" not in sentiment_metadata.tables


def event_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "ts_event": TS,
        "ts_ingested": TS,
        "source": "finnhub",
        "source_uid": "abc",
        "kind": "headline",
        "title": "Fed holds rates",
        "symbols": ["ES"],
        "market_closed": False,
        "dedup_hash": "hash-1",
        "raw": {},
    }
    values.update(overrides)
    return values


def test_dedup_hash_is_unique(tmp_path: Path) -> None:
    """Dedup stojí na hashi (SPEC 3.3) — duplicitu musí zachytit DB, ne jen kód."""
    engine = make_engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(insert(news_events).values(**event_values()))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(news_events).values(**event_values(source="rss_cnbc")))


def test_classifications_are_append_only_versions(tmp_path: Path) -> None:
    """S11: klasifikace se nepřepisuje, přidává se verze; dvojí verze je chyba."""
    engine = make_engine(tmp_path)
    with engine.begin() as conn:
        key = conn.execute(insert(news_events).values(**event_values())).inserted_primary_key
        assert key is not None
        event_id = int(key[0])
        for version, source, direction in ((1, "rule", 0), (2, "llm", 1), (3, "manual", -1)):
            conn.execute(
                insert(news_classifications).values(
                    event_id=event_id,
                    version=version,
                    source=source,
                    category="FED",
                    importance=3,
                    direction=direction,
                    strength=0.8,
                    created_at=TS,
                )
            )
    with engine.connect() as conn:
        rows = conn.execute(
            select(news_classifications.c.version, news_classifications.c.source)
            .where(news_classifications.c.event_id == event_id)
            .order_by(news_classifications.c.version)
        ).fetchall()
    assert [r.source for r in rows] == ["rule", "llm", "manual"]

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            insert(news_classifications).values(
                event_id=event_id, version=2, source="llm", created_at=TS
            )
        )


def test_reactions_carry_contamination_and_deferred_flags(tmp_path: Path) -> None:
    """Anti-šum (SPEC 5.1): okna jdou vyloučit z tréninku podle příznaků."""
    engine = make_engine(tmp_path)
    with engine.begin() as conn:
        key = conn.execute(insert(news_events).values(**event_values())).inserted_primary_key
        assert key is not None
        event_id = int(key[0])
        for window in REACTION_WINDOWS:
            conn.execute(
                insert(news_reactions).values(
                    event_id=event_id,
                    symbol="ES",
                    window_min=window,
                    ret_bp=1.0 * window,
                    range_bp=2.0,
                    vol_z=0.5,
                    # Delší okna chytila další high-impact event
                    contaminated=window >= 15,
                    deferred=False,
                    computed_at=TS,
                )
            )
    with engine.connect() as conn:
        clean = conn.execute(
            select(news_reactions.c.window_min).where(~news_reactions.c.contaminated)
        ).fetchall()
    assert sorted(r.window_min for r in clean) == [1, 5]


def test_crowd_sentiment_is_a_time_series_not_events(tmp_path: Path) -> None:
    """SPEC 5.8: crowd data mají vlastní tabulku a do SentIndexu nevstupují."""
    engine = make_engine(tmp_path)
    with engine.begin() as conn:
        for source, metric, value in (
            ("cnn_fg", "score", 39.4),
            ("reddit", "hot_score", 1200.0),
            ("pcr_gexlens", "pcr_volume", 1.12),
        ):
            conn.execute(
                insert(crowd_sentiment).values(
                    ts=TS, source=source, metric=metric, symbol="", value=value, raw=None
                )
            )
    with engine.connect() as conn:
        rows = conn.execute(select(crowd_sentiment.c.source)).fetchall()
    assert {r.source for r in rows} == {"cnn_fg", "reddit", "pcr_gexlens"}
    # Crowd tabulka nemá vazbu na news_events — je to řada, ne událost
    assert crowd_sentiment.foreign_keys == set()


def test_categories_cover_spec_list() -> None:
    assert "FED" in NEWS_CATEGORIES
    assert "GEOPOLITICS" in NEWS_CATEGORIES
    assert len(NEWS_CATEGORIES) == 10
