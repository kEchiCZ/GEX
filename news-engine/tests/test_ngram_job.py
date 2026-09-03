"""Testy stínové ngram hlavy (#740 fáze 2): klasifikace, izolace, lift."""

import datetime as dt
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    ReactionWindow,
    ensure_sentiment_schema,
    news_classifications,
    news_events,
    news_ngram_shadow,
    news_predictions,
    news_reactions,
    reaction_row_values,
)
from gexlens_news.ngram_job import MIN_EVAL, NgramShadowJob
from gexlens_news.prediction_job import PredictionJob

NOW = dt.datetime(2026, 8, 26, 12, 0, tzinfo=dt.UTC)


def make_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine


def seed_event(
    engine: Engine,
    event_id: int,
    *,
    title: str = "Fed cuts rates",
    ts_event: dt.datetime | None = None,
    ts_ingested: dt.datetime | None = None,
    category: str | None = "FED",
) -> None:
    ts = ts_event or (NOW - dt.timedelta(minutes=event_id))
    with engine.begin() as conn:
        conn.execute(
            insert(news_events),
            [
                {
                    "id": event_id,
                    "ts_event": ts,
                    "ts_ingested": ts_ingested or ts,
                    "source": "rss_news",
                    "kind": "headline",
                    "title": title,
                    "category": category,
                    "symbols": [],
                    "market_closed": False,
                    "dedup_hash": f"hash-{event_id}",
                    "raw": {},
                }
            ],
        )


def seed_rule_classification(engine: Engine, event_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(news_classifications),
            [
                {
                    "event_id": event_id,
                    "version": 1,
                    "source": "rule",
                    "category": "FED",
                    "importance": 2,
                    "direction": 1,
                    "strength": 0.5,
                    "created_at": NOW - dt.timedelta(minutes=1),
                }
            ],
        )


def seed_reaction(
    engine: Engine, event_id: int, ret_bp: float, *, symbol: str = "ES", window: int = 5
) -> None:
    reaction = ReactionWindow(
        window_min=window,
        ret_bp=ret_bp,
        range_bp=abs(ret_bp),
        vol_z=None,
        contaminated=False,
        deferred=False,
        gex_regime=None,
        computed_at=NOW,
    )
    with engine.begin() as conn:
        conn.execute(
            insert(news_reactions).values(
                event_id=event_id, symbol=symbol, **reaction_row_values([reaction])
            )
        )


def trained_job(engine: Engine) -> NgramShadowJob:
    """Job s natrénovaným modelem nad syntetickým korpusem (obejde MIN_TRAIN)."""
    job = NgramShadowJob(engine)
    from gexlens_news.ngram_job import TrainedMagnitude, _feature_row
    from gexlens_news.ngram_model import LogisticModel

    rows = [
        _feature_row("Fed cuts rates", "rss_news", NOW, None),
        _feature_row("Quiet market update", "rss_news", NOW, None),
    ] * 50
    labels = np.array([1.0, 0.0] * 50)
    model = LogisticModel(epochs=3).fit(rows, labels)
    job.trained = TrainedMagnitude(
        model=model, threshold_mid=0.4, threshold_high=0.8, n_train=100, trained_at=NOW
    )
    return job


def test_classify_only_events_with_other_classification(tmp_path: Path) -> None:
    """Ngram verze vzniká jen nad eventy s rule/llm klasifikací — jinak by
    fronta pravidlového passu (#373) event navždy přeskočila."""
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_rule_classification(engine, 1)
    seed_event(engine, 2)  # bez klasifikace — nesmí dostat ngram verzi

    job = trained_job(engine)
    assert job.classify(NOW) == 1
    with engine.connect() as conn:
        rows = conn.execute(
            select(news_classifications.c.event_id, news_classifications.c.version).where(
                news_classifications.c.source == "ngram"
            )
        ).fetchall()
    assert [(row.event_id, row.version) for row in rows] == [(1, 2)]
    # Opakovaný běh nic nepřidá (event už ngram verzi má)
    assert job.classify(NOW) == 0


def test_classify_is_shadow_only(tmp_path: Path) -> None:
    """direction=0, strength=P; denormalizace v news_events zůstává pravidlová."""
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_rule_classification(engine, 1)
    job = trained_job(engine)
    job.classify(NOW)
    with engine.connect() as conn:
        ngram = conn.execute(
            select(news_classifications).where(news_classifications.c.source == "ngram")
        ).one()
        event = conn.execute(select(news_events).where(news_events.c.id == 1)).one()
    assert ngram.direction == 0
    assert 0.0 <= float(ngram.strength) <= 1.0
    assert ngram.importance in (1, 2, 3)
    # Shadow se nesmí propsat do eventu (sentiment_source drží pravidla)
    assert event.sentiment_source != "ngram"


def test_prediction_job_ignores_ngram(tmp_path: Path) -> None:
    """Ngram klasifikace nezakládá predikci — direction=0 by plnilo outcomes
    prohrami a přes load_weight_map přepisovalo váhy pravidel."""
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_rule_classification(engine, 1)
    trained_job(engine).classify(NOW)
    PredictionJob(engine).create_predictions(NOW)
    with engine.connect() as conn:
        predictors = {row.predictor for row in conn.execute(select(news_predictions.c.predictor))}
    assert predictors == {"rule"}


def test_evaluate_lift_and_subsets(tmp_path: Path) -> None:
    """Lift horního decilu + oddělení live/backfill přes ts_ingested − ts_event."""
    engine = make_db(tmp_path)
    rng = np.random.default_rng(1)
    # MIN_EVAL živých vzorků: strength koreluje s |reakcí| (model má edge);
    # kategorie střídavě, aby baseline měl co řadit
    for i in range(MIN_EVAL):
        event_id = i + 1
        big = i % 10 == 0  # 10 % velkých pohybů
        ts_event = NOW - dt.timedelta(hours=2, minutes=i)
        seed_event(
            engine,
            event_id,
            ts_event=ts_event,
            ts_ingested=ts_event + dt.timedelta(minutes=1),  # live: lag 1 min
            category="FED" if big else "OTHER",
        )
        with engine.begin() as conn:
            conn.execute(
                insert(news_classifications),
                [
                    {
                        "event_id": event_id,
                        "version": 1,
                        "source": "ngram",
                        "category": "FED" if big else "OTHER",
                        "importance": 3 if big else 1,
                        "direction": 0,
                        "strength": 0.9 if big else float(rng.uniform(0.1, 0.5)),
                        # Prospektivní: klasifikace vznikla před uzavřením okna
                        "created_at": ts_event + dt.timedelta(minutes=2),
                    }
                ],
            )
        seed_reaction(engine, event_id, 20.0 if big else 2.0)
    # Postdikce: čerstvý event klasifikovaný až po uzavření okna reakce —
    # do subsetu `live` nesmí (leak: reakce mohla být v tréninku), do `all` ano
    seed_event(
        engine,
        MIN_EVAL + 2,
        ts_event=NOW - dt.timedelta(hours=3),
        ts_ingested=NOW - dt.timedelta(hours=3),
        category="FED",
    )
    with engine.begin() as conn:
        conn.execute(
            insert(news_classifications),
            [
                {
                    "event_id": MIN_EVAL + 2,
                    "version": 1,
                    "source": "ngram",
                    "category": "FED",
                    "importance": 1,
                    "direction": 0,
                    "strength": 0.2,
                    "created_at": NOW,  # 3 h po eventu = postdikce
                }
            ],
        )
    seed_reaction(engine, MIN_EVAL + 2, 5.0)
    # Jeden backfill vzorek — pod MIN_EVAL, subset se nezapíše
    seed_event(
        engine,
        MIN_EVAL + 1,
        ts_event=NOW - dt.timedelta(days=400),
        ts_ingested=NOW,
        category="FED",
    )

    job = NgramShadowJob(engine)
    assert job.evaluate(NOW) == 2  # ES: all + live (NQ reakce nejsou)
    with engine.connect() as conn:
        rows = {row.subset: row for row in conn.execute(select(news_ngram_shadow))}
    assert set(rows) == {"all", "live"}
    live = rows["live"]
    assert live.n == MIN_EVAL  # postdikce vyřazena
    assert rows["all"].n == MIN_EVAL + 1  # ...ale v `all` zůstává
    # Horní decil podle strength = přesně velké pohyby → lift výrazně > 1
    assert live.lift > 2.0
    # Baseline (kategorie) tu velké pohyby taky najde — lift srovnatelný
    assert live.baseline_lift > 2.0
    assert live.mean_bp > 0


def test_evaluate_full_replace_clears_stale_subsets(tmp_path: Path) -> None:
    """Subset pod MIN_EVAL nesmí v tabulce nechat stale řádek z minulého běhu."""
    engine = make_db(tmp_path)
    with engine.begin() as conn:
        conn.execute(
            insert(news_ngram_shadow),
            [
                {
                    "symbol": "ES",
                    "window_min": 5,
                    "subset": "live",
                    "n": 282,
                    "lift": 1.9,
                    "baseline_lift": 1.27,
                    "top_decile_mean_bp": 10.0,
                    "mean_bp": 5.0,
                    "model_n_train": 100,
                    "computed_at": NOW - dt.timedelta(days=1),
                }
            ],
        )
    job = NgramShadowJob(engine)
    assert job.evaluate(NOW) == 0  # žádná kvalifikovaná data
    with engine.connect() as conn:
        assert conn.execute(select(news_ngram_shadow)).fetchall() == []


def test_run_retrains_once_a_day_and_survives_thin_db(tmp_path: Path) -> None:
    """Studená DB: retrénink se vzdá (MIN_TRAIN), run nespadne a nic nezapíše."""
    engine = make_db(tmp_path)
    seed_event(engine, 1)
    seed_rule_classification(engine, 1)
    job = NgramShadowJob(engine)
    assert job.run(NOW) == 0  # bez modelu se neklasifikuje
    assert job.trained is None
    with engine.connect() as conn:
        assert (
            conn.execute(
                select(news_classifications).where(news_classifications.c.source == "ngram")
            ).fetchall()
            == []
        )
