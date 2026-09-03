"""Testy drift hlídky (#403): binomický test, nálezy, anti-spam alertů."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, update
from sqlalchemy.engine import Engine

from gexlens_engine.storage.meta import meta_metadata
from gexlens_engine.storage.sentiment import (
    ReactionWindow,
    ensure_sentiment_schema,
    news_events,
    news_model_stats,
    news_reactions,
    reaction_row_values,
)
from gexlens_engine.storage.setups_store import SetupsRepository, setups_table
from gexlens_news.drift import RECENT_N, DriftJob, binomial_p_at_most

NOW = dt.datetime(2026, 7, 31, 2, 0, tzinfo=dt.UTC)


def test_binomial_p_at_most() -> None:
    assert binomial_p_at_most(0, 10, 0.5) == pytest.approx(0.5**10)
    assert binomial_p_at_most(10, 10, 0.5) == pytest.approx(1.0)
    # 5 zásahů z 20 při dlouhodobých 61 % je významný pokles
    assert binomial_p_at_most(5, 20, 0.61) < 0.01
    # 11 z 20 při 61 % je v normě
    assert binomial_p_at_most(11, 20, 0.61) > 0.2
    assert binomial_p_at_most(3, 0, 0.5) == 1.0  # degenerované vstupy


def make_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'drift.sqlite'}")
    ensure_sentiment_schema(engine)
    meta_metadata.create_all(engine)
    SetupsRepository(engine).ensure_schema()
    return engine


def seed_bucket(engine: Engine, *, hit_rate: float) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(news_model_stats),
            [
                {
                    "regime": "all",
                    "category": "FED",
                    "importance": 3,
                    "surprise_bucket": "none",
                    "deferred": False,
                    "window_min": 5,
                    "symbol": "ES",
                    "n": 100,
                    "ret_mean_bp": 5.0,
                    "ret_median_bp": 5.0,
                    "ret_sigma_bp": 3.0,
                    "hit_rate": hit_rate,
                    "hit_rate_lb": 0.55,
                    "computed_at": NOW,
                }
            ],
        )


def seed_recent_reactions(engine: Engine, *, hits: int, total: int) -> None:
    """Posledních `total` reakcí bucketu: `hits` ve směru klasifikace."""
    with engine.begin() as conn:
        for index in range(total):
            event_id = 1000 + index
            hit = index < hits
            conn.execute(
                insert(news_events),
                [
                    {
                        "id": event_id,
                        "ts_event": NOW - dt.timedelta(hours=index + 1),
                        "ts_ingested": NOW,
                        "source": "rss_news",
                        "kind": "headline",
                        "category": "FED",
                        "importance": 3,
                        "title": f"e{event_id}",
                        "symbols": ["ES"],
                        "market_closed": False,
                        "sentiment_dir": 1,
                        "dedup_hash": f"h{event_id}",
                        "raw": {},
                    }
                ],
            )
            window = ReactionWindow(
                window_min=5,
                ret_bp=4.0 if hit else -4.0,
                range_bp=6.0,
                vol_z=None,
                contaminated=False,
                deferred=False,
                gex_regime=None,
                computed_at=NOW,
            )
            conn.execute(
                insert(news_reactions).values(
                    event_id=event_id, symbol="ES", **reaction_row_values([window])
                )
            )


def test_drift_fires_once_for_degraded_bucket(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_bucket(engine, hit_rate=0.61)
    seed_recent_reactions(engine, hits=5, total=RECENT_N)  # 25 % vs. 61 %
    job = DriftJob(engine)

    alerts = job.run(NOW)
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "drift"
    assert "FED" in alerts[0]["message"]
    assert "25 %" in alerts[0]["message"] or "25%" in alerts[0]["message"]

    # Táž situace další noc → nález trvá, ale alert se neopakuje (anti-spam)
    assert job.run(NOW + dt.timedelta(days=1)) == []


def test_no_drift_when_recent_matches_history(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_bucket(engine, hit_rate=0.61)
    seed_recent_reactions(engine, hits=12, total=RECENT_N)  # 60 % ≈ 61 %
    assert DriftJob(engine).run(NOW) == []


def test_no_drift_without_enough_recent_samples(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    seed_bucket(engine, hit_rate=0.61)
    seed_recent_reactions(engine, hits=0, total=5)  # málo dat → žádný test
    assert DriftJob(engine).run(NOW) == []


def seed_closed_setup(
    repository: SetupsRepository,
    *,
    win: bool,
    created_ts: dt.datetime,
    closed_ts: dt.datetime,
    template: str = "wall_bounce",
) -> int:
    setup_id = repository.create(
        symbol="ES",
        expiry="20260731",
        template=template,
        direction="long",
        created_ts=created_ts,
        entry=7400.0,
        target=7420.0,
        stop=7390.0,
        confidence=1,
        reason="test",
        context={},
    )
    repository.close(
        setup_id,
        status="closed_target" if win else "closed_stop",
        closed_ts=closed_ts,
        outcome_r=1.0 if win else -1.0,
        mfe=1.0,
        mae=0.5,
    )
    return setup_id


def test_setup_template_drift(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    repository = SetupsRepository(engine)
    # 50 uzavřených: starších 30 se 70% úspěšností, posledních 20 jen 4 výhry
    outcomes = [True] * 21 + [False] * 9 + [True] * 4 + [False] * 16
    for index, win in enumerate(outcomes):
        seed_closed_setup(
            repository,
            win=win,
            created_ts=NOW - dt.timedelta(days=len(outcomes) - index),
            closed_ts=NOW - dt.timedelta(days=len(outcomes) - index, hours=-2),
        )
    alerts = DriftJob(engine).run(NOW)
    assert len(alerts) == 1
    assert "wall_bounce" in alerts[0]["message"]


def test_setup_drift_ignoruje_stare_mechanics_verze(tmp_path: Path) -> None:
    """#496: v1 výsledky (včetně incidentu se zmrzlými Greeks, ADR-0015) nesmí
    vstupovat do baseline aktuálního systému — konvence z #311."""
    engine = make_db(tmp_path)
    repository = SetupsRepository(engine)
    # Aktuální mechanika: 50 uzavřených se stabilní úspěšností ~70 % → žádný drift
    outcomes = [True] * 21 + [False] * 9 + [True] * 14 + [False] * 6
    for index, win in enumerate(outcomes):
        seed_closed_setup(
            repository,
            win=win,
            created_ts=NOW - dt.timedelta(days=100 - index),
            closed_ts=NOW - dt.timedelta(days=100 - index, hours=-2),
        )
    # v1: 20 NEJNOVĚJI uzavřených, samé stopky — bez filtru by vytlačily
    # klouzavé okno a spustily falešný drift alert
    v1_ids = [
        seed_closed_setup(
            repository,
            win=False,
            created_ts=NOW - dt.timedelta(days=2, minutes=index),
            closed_ts=NOW - dt.timedelta(days=1, minutes=-index),
        )
        for index in range(RECENT_N)
    ]
    with engine.begin() as conn:
        conn.execute(
            update(setups_table).where(setups_table.c.id.in_(v1_ids)).values(mechanics_version=1)
        )

    assert DriftJob(engine).run(NOW) == []


def test_setup_drift_okno_se_ridi_casem_uzavreni(tmp_path: Path) -> None:
    """#496: „posledních 20" jsou poslední UZAVŘENÉ, ne poslední založené.

    Dvacet ztrát založených nejdřív, ale uzavřených nejpozději, musí tvořit
    klouzavé okno — řazení podle created_ts by je schovalo do baseline.
    """
    engine = make_db(tmp_path)
    repository = SetupsRepository(engine)
    # Baseline: 30 výsledků se 70 % — uzavřené dávno
    for index in range(30):
        seed_closed_setup(
            repository,
            win=index < 21,
            created_ts=NOW - dt.timedelta(days=50, minutes=-index),
            closed_ts=NOW - dt.timedelta(days=30, minutes=-index),
        )
    # 20 ztrát: založené PŘED baseline, ale uzavřené jako poslední
    for index in range(RECENT_N):
        seed_closed_setup(
            repository,
            win=False,
            created_ts=NOW - dt.timedelta(days=100, minutes=-index),
            closed_ts=NOW - dt.timedelta(days=1, minutes=-index),
        )

    alerts = DriftJob(engine).run(NOW)
    assert len(alerts) == 1
    assert "wall_bounce" in alerts[0]["message"]
    assert "20 výsledků 0%" in alerts[0]["message"]  # klouzavé okno = samé ztráty
