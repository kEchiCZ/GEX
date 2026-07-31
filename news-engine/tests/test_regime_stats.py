"""Testy režimově podmíněných statistik (#402): agregace, migrace, preference."""

import datetime as dt
from pathlib import Path

from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    news_events,
    news_model_stats,
    news_reactions,
    sentiment_daily,
)
from gexlens_news.model_stats import BucketStats as ModelBucketStats
from gexlens_news.model_stats import ReactionSample, aggregate_by_regime
from gexlens_news.model_stats_job import ModelStatsJob
from gexlens_news.signal_engine import BucketStats, gate_passes
from gexlens_news.signal_job import SignalJob

NOW = dt.datetime(2026, 7, 31, 14, 0, tzinfo=dt.UTC)


def sample(**overrides: object) -> ReactionSample:
    values: dict[str, object] = dict(
        category="FED",
        importance=3,
        surprise_z=None,
        symbol="ES",
        window_min=5,
        ret_bp=5.0,
        contaminated=False,
        deferred=False,
        sentiment_dir=1,
    )
    values.update(overrides)
    return ReactionSample(**values)  # type: ignore[arg-type]


def test_aggregate_by_regime_parallel_views() -> None:
    """Podmíněné pohledy vedle 'all' — vzorky bez režimu jen do nepodmíněného."""
    samples = [
        sample(state="RiskOn", gex_regime="negative"),
        sample(state="RiskOn", gex_regime=None),
        sample(state=None, gex_regime="positive", ret_bp=-3.0),
    ]
    rows = aggregate_by_regime(samples)
    by_regime: dict[str, list[ModelBucketStats]] = {}
    for regime, stats in rows:
        by_regime.setdefault(regime, []).append(stats)
    assert {s.n for s in by_regime["all"]} == {3}  # všechny vzorky
    assert {s.n for s in by_regime["RiskOn"]} == {2}
    assert {s.n for s in by_regime["gamma_negative"]} == {1}
    assert {s.n for s in by_regime["gamma_positive"]} == {1}
    assert "Neutral" not in by_regime  # žádný vzorek → žádný řádek


def make_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'regime.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine


def test_migration_recreates_model_stats_and_extends_reactions(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'old.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE news_model_stats (category TEXT, n INT)"))
        conn.execute(text("CREATE TABLE news_reactions (event_id INT, ret_bp REAL)"))
        conn.execute(text("INSERT INTO news_reactions VALUES (1, 5.0)"))
    ensure_sentiment_schema(engine)
    from sqlalchemy import inspect

    inspector = inspect(engine)
    assert "regime" in {c["name"] for c in inspector.get_columns("news_model_stats")}
    assert "gex_regime" in {c["name"] for c in inspector.get_columns("news_reactions")}
    with engine.connect() as conn:
        # Naměřená data přežila (ADD COLUMN, ne drop)
        assert conn.execute(text("SELECT count(*) FROM news_reactions")).scalar() == 1


def seed_event_with_reaction(engine: Engine, event_id: int, *, gex_regime: str | None) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(news_events),
            [
                {
                    "id": event_id,
                    "ts_event": NOW - dt.timedelta(days=1),
                    "ts_ingested": NOW,
                    "source": "rss_news",
                    "kind": "headline",
                    "category": "FED",
                    "importance": 3,
                    "title": f"event {event_id}",
                    "symbols": ["ES"],
                    "market_closed": False,
                    "sentiment_dir": 1,
                    "dedup_hash": f"h{event_id}",
                    "raw": {},
                }
            ],
        )
        conn.execute(
            insert(news_reactions),
            [
                {
                    "event_id": event_id,
                    "symbol": "ES",
                    "window_min": 5,
                    "ret_bp": 6.0,
                    "range_bp": 8.0,
                    "vol_z": None,
                    "contaminated": False,
                    "deferred": False,
                    "gex_regime": gex_regime,
                    "computed_at": NOW,
                }
            ],
        )


def test_model_stats_job_writes_regime_rows(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    with engine.begin() as conn:
        conn.execute(
            insert(sentiment_daily),
            [
                {
                    "date": NOW.date() - dt.timedelta(days=offset),
                    "symbol": "ES",
                    "open": 0.1,
                    "high": 0.2,
                    "low": -0.2,
                    "close": 0.1,
                    "update_time": NOW,
                }
                for offset in range(1, 4)
            ],
        )
    seed_event_with_reaction(engine, 1, gex_regime="negative")
    seed_event_with_reaction(engine, 2, gex_regime=None)
    ModelStatsJob(engine).run(NOW)
    with engine.connect() as conn:
        rows = conn.execute(select(news_model_stats)).fetchall()
    regimes = {row.regime for row in rows}
    assert "all" in regimes and "gamma_negative" in regimes
    all_row = next(row for row in rows if row.regime == "all")
    neg_row = next(row for row in rows if row.regime == "gamma_negative")
    assert all_row.n == 2 and neg_row.n == 1


def test_signal_bucket_prefers_state_view_only_with_gate(tmp_path: Path) -> None:
    engine = make_db(tmp_path)

    def bucket_row(regime: str, n: int, lb: float, mean: float) -> dict[str, object]:
        return {
            "regime": regime,
            "category": "FED",
            "importance": 3,
            "surprise_bucket": "none",
            "deferred": False,
            "window_min": 5,
            "symbol": "ES",
            "n": n,
            "ret_mean_bp": mean,
            "ret_median_bp": mean,
            "ret_sigma_bp": 3.0,
            "hit_rate": 0.6,
            "hit_rate_lb": lb,
            "computed_at": NOW,
        }

    with engine.begin() as conn:
        conn.execute(
            insert(news_model_stats),
            [
                bucket_row("all", 60, 0.55, 4.0),
                bucket_row("RiskOn", 40, 0.62, 9.0),
                bucket_row("RiskOff", 8, 0.9, -5.0),  # mělký → gate neprojde
            ],
        )
    job = SignalJob(engine, tmp_path, symbols=("ES",))
    from gexlens_news.signal_engine import SignalEvent

    event = SignalEvent(
        event_id=1,
        ts_event=NOW,
        category="FED",
        importance=3,
        score=0.5,
        surprise_bucket="none",
        deferred=False,
        classification_version=1,
    )
    # RiskOn: podmíněný bucket má gate → použije se (mean 9, regime RiskOn)
    risk_on = job._bucket_stats(event, "ES", "RiskOn")
    assert risk_on is not None and risk_on.regime == "RiskOn" and risk_on.ret_mean_bp == 9.0
    assert gate_passes(risk_on)
    # RiskOff: podmíněný mělký → fallback na 'all'
    risk_off = job._bucket_stats(event, "ES", "RiskOff")
    assert risk_off is not None and risk_off.regime == "all" and risk_off.n == 60
    # Neutral: podmíněný neexistuje → 'all'
    neutral = job._bucket_stats(event, "ES", "Neutral")
    assert neutral is not None and neutral.regime == "all"
    assert isinstance(neutral, BucketStats)
