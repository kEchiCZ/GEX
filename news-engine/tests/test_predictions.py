"""Testy predikcí, per-okno vyhodnocení a vah (#282)."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    ReactionWindow,
    ensure_sentiment_schema,
    news_classifications,
    news_events,
    news_prediction_outcomes,
    news_predictions,
    news_reactions,
    news_weights,
    reaction_row_values,
)
from gexlens_news.prediction_job import PredictionJob, event_weight, load_weight_map
from gexlens_news.predictions import (
    MIN_SAMPLES_FOR_WEIGHT,
    WEIGHT_EDGE_GAIN,
    WEIGHT_MAX,
    WEIGHT_MIN,
    Outcome,
    compute_weights,
    weight_from_hit_rate,
)

NOW = dt.datetime(2026, 7, 28, 20, 0, tzinfo=dt.UTC)


def outcome(correct: bool, *, category: str = "FED", predictor: str = "rule") -> Outcome:
    return Outcome(
        category=category,
        predictor=predictor,
        window_min=5,
        predicted_dir=1,
        realized_ret_bp=1.0 if correct else -1.0,
    )


# ── Váha z úspěšnosti ──────────────────────────────────────────────


def test_coin_flip_is_exactly_neutral() -> None:
    """ADR-0036 (#1150): LB = 0,5 dává PŘESNĚ 1,0 — totéž co chybějící váha.

    Dosažení MIN_SAMPLES tak nedělá skok a kategorie bez edge se z indexu
    nevymaže (původní `max(0, 2·LB − 1)` ji nulovalo, index byl identicky 0).
    """
    assert weight_from_hit_rate(0.5) == 1.0


def test_edge_above_coin_flip_amplifies_up_to_cap() -> None:
    assert weight_from_hit_rate(0.6) == pytest.approx(1.0 + WEIGHT_EDGE_GAIN * 0.2)
    assert weight_from_hit_rate(0.75) == WEIGHT_MAX  # 1 + 2·0,5 = 2,0 = strop
    assert weight_from_hit_rate(1.0) == WEIGHT_MAX


def test_worse_than_coin_flip_is_damped_never_zero_nor_negative() -> None:
    """Horší než mince tlumí (podlaha WEIGHT_MIN) — nikdy nula, nikdy otočené znaménko."""
    assert weight_from_hit_rate(0.4) == pytest.approx(1.0 - WEIGHT_EDGE_GAIN * 0.2)
    assert weight_from_hit_rate(0.2) == WEIGHT_MIN
    assert weight_from_hit_rate(0.0) == WEIGHT_MIN
    assert WEIGHT_MIN > 0.0


def test_small_samples_get_no_weight_at_all() -> None:
    """Chybějící váha znamená „zatím nevíme"; volající použije neutrální 1.0."""
    few = [outcome(True) for _ in range(MIN_SAMPLES_FOR_WEIGHT - 1)]
    assert compute_weights(few) == []


def test_weight_reflects_sample_size_at_same_hit_rate() -> None:
    """Stejná úspěšnost, ale víc vzorků = vyšší důvěra = vyšší váha.

    60 % trefy: z 20 vzorků je Wilson LB pod mincí (tlumí), z 200 nad ní
    (zesiluje) — obojí pod stropem WEIGHT_MAX, aby se rozdíl neořízl.
    """

    def batch(n: int) -> list[Outcome]:
        return [outcome(i % 5 < 3) for i in range(n)]

    small = compute_weights(batch(MIN_SAMPLES_FOR_WEIGHT))[0]
    large = compute_weights(batch(200))[0]
    assert small.hit_rate == large.hit_rate == 0.6
    assert small.weight < 1.0 < large.weight < WEIGHT_MAX


def test_only_primary_window_and_directional_predictions_count() -> None:
    mixed = [outcome(True) for _ in range(MIN_SAMPLES_FOR_WEIGHT)]
    mixed += [
        Outcome(category="FED", predictor="rule", window_min=60, predicted_dir=1, realized_ret_bp=1)
        for _ in range(50)
    ]
    mixed += [
        Outcome(category="FED", predictor="rule", window_min=5, predicted_dir=0, realized_ret_bp=1)
        for _ in range(50)
    ]
    weights = compute_weights(mixed)
    assert len(weights) == 1
    assert weights[0].n == MIN_SAMPLES_FOR_WEIGHT  # jen primární okno se směrem


def test_categories_and_predictors_are_separate() -> None:
    items = [outcome(True, category="FED") for _ in range(MIN_SAMPLES_FOR_WEIGHT)]
    items += [outcome(False, category="TECH") for _ in range(MIN_SAMPLES_FOR_WEIGHT)]
    items += [outcome(True, category="FED", predictor="llm") for _ in range(MIN_SAMPLES_FOR_WEIGHT)]
    weights = {(w.category, w.predictor): w for w in compute_weights(items)}
    assert len(weights) == 3
    assert weights[("FED", "rule")].weight > 1.0
    assert weights[("TECH", "rule")].weight == WEIGHT_MIN  # samé minutí → podlaha, ne nula


# ── Job nad DB ─────────────────────────────────────────────────────


def seed(engine: Engine, *, direction: int, ret_bp: float, contaminated: bool = False) -> int:
    with engine.begin() as conn:
        key = conn.execute(
            insert(news_events).values(
                ts_event=NOW - dt.timedelta(hours=2),
                ts_ingested=NOW - dt.timedelta(hours=2),
                source="rss_news",
                kind="headline",
                title=f"zprava-{ret_bp}-{contaminated}",
                category="FED",
                importance=3,
                symbols=[],
                market_closed=False,
                dedup_hash=f"h-{ret_bp}-{contaminated}",
                raw={},
            )
        ).inserted_primary_key
        assert key is not None
        event_id = int(key[0])
        conn.execute(
            insert(news_classifications).values(
                event_id=event_id,
                version=1,
                source="rule",
                category="FED",
                importance=3,
                direction=direction,
                strength=0.4,
                created_at=NOW,
            )
        )
        window = ReactionWindow(
            window_min=5,
            ret_bp=ret_bp,
            range_bp=10.0,
            vol_z=None,
            contaminated=contaminated,
            deferred=False,
            gex_regime=None,
            computed_at=NOW,
        )
        conn.execute(
            insert(news_reactions).values(
                event_id=event_id, symbol="ES", **reaction_row_values([window])
            )
        )
    return event_id


def make(tmp_path: Path) -> tuple[Engine, PredictionJob]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine, PredictionJob(engine)


def test_prediction_carries_classification_version_and_is_created_once(tmp_path: Path) -> None:
    engine, job = make(tmp_path)
    seed(engine, direction=1, ret_bp=5.0)

    assert job.create_predictions(NOW) == 1
    assert job.create_predictions(NOW) == 0  # immutable, nezakládá se znovu

    with engine.connect() as conn:
        row = conn.execute(select(news_predictions)).fetchone()
    assert row is not None
    assert row.classification_version == 1
    assert row.predictor == "rule"
    assert row.predicted_dir == 1


def test_outcome_is_written_per_window_and_skips_contaminated(tmp_path: Path) -> None:
    engine, job = make(tmp_path)
    seed(engine, direction=1, ret_bp=5.0)  # trefa
    seed(engine, direction=1, ret_bp=-5.0, contaminated=True)  # kontaminované okno
    job.create_predictions(NOW)

    assert job.evaluate(NOW) == 1  # kontaminované se nevyhodnocuje
    assert job.evaluate(NOW) == 0  # idempotentní

    with engine.connect() as conn:
        row = conn.execute(select(news_prediction_outcomes)).fetchone()
    assert row is not None
    assert row.window_min == 5
    assert row.realized_dir == 1
    assert row.correct is True


def test_weights_land_in_db_and_map_defaults_to_neutral(tmp_path: Path) -> None:
    engine, job = make(tmp_path)
    for i in range(MIN_SAMPLES_FOR_WEIGHT):
        seed(engine, direction=1, ret_bp=float(i + 1))  # samé trefy
    job.run(NOW)

    with engine.connect() as conn:
        rows = conn.execute(select(news_weights)).fetchall()
    assert len(rows) == 1
    assert rows[0].category == "FED"
    assert rows[0].weight > 1.0

    weights = load_weight_map(engine)
    assert weights[("FED", "rule")] > 1.0
    # Kategorie bez vyhodnocení v mapě není → volající použije 1.0
    assert ("GEOPOLITICS", "rule") not in weights
    assert event_weight(weights, "GEOPOLITICS", "rule") == 1.0
    assert event_weight(weights, "FED", None) == 1.0  # event bez zdroje skóre


def test_weight_map_keeps_predictors_apart(tmp_path: Path) -> None:
    """#1150: rule i llm mají vlastní řádek — dict per kategorie nechal vyhrát poslední."""
    engine, _job = make(tmp_path)
    with engine.begin() as conn:
        conn.execute(
            insert(news_weights),
            [
                {
                    "category": "FED",
                    "predictor": predictor,
                    "window_min": 5,
                    "symbol": "ES",
                    "n": 50,
                    "hit_rate": 0.5,
                    "hit_rate_lb": 0.4,
                    "weight": weight,
                    "computed_at": NOW,
                }
                for predictor, weight in (("rule", 0.5), ("llm", 1.5))
            ],
        )

    weights = load_weight_map(engine, "ES")
    assert weights == {("FED", "rule"): 0.5, ("FED", "llm"): 1.5}
    assert event_weight(weights, "FED", "rule") == 0.5
    assert event_weight(weights, "FED", "llm") == 1.5
    assert load_weight_map(engine, "NQ") == {}
