"""Testy empirického modelu (#279): bucketování, anti-šum, hit-rate, lookup."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, select

from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    news_events,
    news_model_stats,
    news_reactions,
)
from gexlens_news.model_stats import (
    ReactionSample,
    aggregate_samples,
    lookup,
    surprise_bucket,
)
from gexlens_news.model_stats_job import ModelStatsJob

NOW = dt.datetime(2026, 7, 28, 22, 0, tzinfo=dt.UTC)


def sample(
    ret_bp: float,
    *,
    category: str | None = "MACRO_INFLATION",
    importance: int | None = 3,
    surprise_z: float | None = None,
    contaminated: bool = False,
    deferred: bool = False,
    sentiment_dir: int | None = None,
    window_min: int = 5,
    symbol: str = "ES",
) -> ReactionSample:
    return ReactionSample(
        category=category,
        importance=importance,
        surprise_z=surprise_z,
        symbol=symbol,
        window_min=window_min,
        ret_bp=ret_bp,
        contaminated=contaminated,
        deferred=deferred,
        sentiment_dir=sentiment_dir,
    )


# ── Bucketování překvapení ─────────────────────────────────────────


def test_surprise_bucket_thresholds() -> None:
    assert surprise_bucket(None) == "none"  # headline forecast nemá
    assert surprise_bucket(0.2) == "flat"  # v šumu konsensu
    assert surprise_bucket(-0.4) == "flat"
    assert surprise_bucket(1.0) == "pos_small"
    assert surprise_bucket(-1.0) == "neg_small"
    assert surprise_bucket(2.5) == "pos_large"
    assert surprise_bucket(-3.0) == "neg_large"


# ── Agregace a anti-šum ────────────────────────────────────────────


def test_aggregate_gives_expected_direction_and_spread() -> None:
    stats = aggregate_samples([sample(10.0), sample(20.0), sample(30.0)])
    assert len(stats) == 1
    bucket = stats[0]
    assert bucket.n == 3
    assert bucket.ret_mean_bp == pytest.approx(20.0)
    assert bucket.ret_median_bp == pytest.approx(20.0)
    assert bucket.ret_sigma_bp > 0
    assert bucket.expected_direction == 1


def test_contaminated_windows_are_excluded() -> None:
    """Okno s cizím high-impact eventem neměří reakci na tuhle zprávu."""
    stats = aggregate_samples([sample(10.0), sample(1000.0, contaminated=True), sample(10.0)])
    assert stats[0].n == 2
    assert stats[0].ret_mean_bp == pytest.approx(10.0)  # výstřelek se nezapočítal


def test_deferred_forms_its_own_bucket() -> None:
    """Gap na open má jinou dynamiku než okamžitá reakce — nemíchat."""
    stats = aggregate_samples([sample(5.0), sample(120.0, deferred=True)])
    assert len(stats) == 2
    by_deferred = {s.key.deferred: s for s in stats}
    assert by_deferred[False].ret_mean_bp == pytest.approx(5.0)
    assert by_deferred[True].ret_mean_bp == pytest.approx(120.0)


def test_unclassified_events_are_skipped() -> None:
    """Bez kategorie/importance event nepatří nikam — míchat do OTHER by ředilo."""
    assert aggregate_samples([sample(10.0, category=None)]) == []
    assert aggregate_samples([sample(10.0, importance=None)]) == []


def test_buckets_split_by_window_symbol_and_surprise() -> None:
    stats = aggregate_samples(
        [
            sample(5.0, window_min=1),
            sample(5.0, window_min=5),
            sample(5.0, symbol="NQ"),
            sample(5.0, surprise_z=2.0),
        ]
    )
    assert len(stats) == 4


# ── Hit-rate a spolehlivost ────────────────────────────────────────


def test_hit_rate_counts_only_classified_events() -> None:
    """Neklasifikovaný event nemá s čím porovnat — nesmí úspěšnost stlačit."""
    stats = aggregate_samples(
        [
            sample(10.0, sentiment_dir=1),  # trefa
            sample(-10.0, sentiment_dir=1),  # vedle
            sample(50.0),  # bez klasifikace → mimo hit-rate
        ]
    )
    bucket = stats[0]
    assert bucket.n == 3  # do rozdělení reakcí patří všechny
    assert bucket.hit_rate == pytest.approx(0.5)  # ale hit-rate jen ze dvou
    assert bucket.hit_rate_lb is not None and bucket.hit_rate_lb < 0.5


def test_hit_rate_is_none_without_classification() -> None:
    stats = aggregate_samples([sample(10.0), sample(20.0)])
    assert stats[0].hit_rate is None
    assert stats[0].hit_rate_lb is None


def test_wilson_lower_bound_punishes_small_samples() -> None:
    """Stejná bodová úspěšnost, ale dolní mez roste s počtem vzorků."""
    few = aggregate_samples([sample(10.0, sentiment_dir=1)] * 3)[0]
    many = aggregate_samples([sample(10.0, sentiment_dir=1)] * 60)[0]
    assert few.hit_rate == many.hit_rate == 1.0
    assert few.hit_rate_lb is not None and many.hit_rate_lb is not None
    assert few.hit_rate_lb < many.hit_rate_lb


# ── Lookup (jádro „učení" fáze 1) ──────────────────────────────────


def test_lookup_finds_matching_bucket_only() -> None:
    stats = aggregate_samples([sample(12.0, surprise_z=2.0)])
    found = lookup(
        stats,
        category="MACRO_INFLATION",
        importance=3,
        surprise_z=1.8,  # jiný z, ale stejný bucket (pos_large)
        deferred=False,
        window_min=5,
        symbol="ES",
    )
    assert found is not None and found.ret_mean_bp == pytest.approx(12.0)

    assert (
        lookup(
            stats,
            category="FED",
            importance=3,
            surprise_z=2.0,
            deferred=False,
            window_min=5,
            symbol="ES",
        )
        is None
    )


# ── Job nad DB ─────────────────────────────────────────────────────


def test_job_recomputes_from_scratch(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    with engine.begin() as conn:
        key = conn.execute(
            insert(news_events).values(
                ts_event=NOW,
                ts_ingested=NOW,
                source="forexfactory",
                kind="scheduled",
                title="USD CPI m/m",
                category="MACRO_INFLATION",
                importance=3,
                surprise_z=2.0,
                symbols=[],
                market_closed=False,
                dedup_hash="cpi",
                raw={},
            )
        ).inserted_primary_key
        assert key is not None
        event_id = int(key[0])
        conn.execute(
            insert(news_reactions),
            [
                {
                    "event_id": event_id,
                    "symbol": "ES",
                    "window_min": window,
                    "ret_bp": 10.0 * window,
                    "range_bp": 20.0,
                    "vol_z": None,
                    "contaminated": window == 60,  # nejdelší okno kontaminované
                    "deferred": False,
                    "computed_at": NOW,
                }
                for window in (1, 5, 15, 60)
            ],
        )

    job = ModelStatsJob(engine)
    assert job.run(NOW) == 3  # kontaminované okno vypadlo

    with engine.connect() as conn:
        rows = conn.execute(select(news_model_stats)).fetchall()
    assert {r.window_min for r in rows} == {1, 5, 15}
    assert all(r.surprise_bucket == "pos_large" for r in rows)
    assert all(r.hit_rate is None for r in rows)  # klasifikace přijde v N3

    # Opakovaný běh tabulku nahradí, ne zduplikuje
    assert job.run(NOW) == 3
    with engine.connect() as conn:
        assert len(conn.execute(select(news_model_stats)).fetchall()) == 3
