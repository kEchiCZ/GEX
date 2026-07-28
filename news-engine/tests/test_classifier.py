"""Testy pravidlového klasifikátoru a znaménkových konvencí (#280)."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, select

from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    news_classifications,
    news_events,
)
from gexlens_news.classification_job import RuleClassificationJob
from gexlens_news.classifier import classify, classify_direction, classify_importance
from gexlens_news.conventions import (
    ConventionOutcome,
    check_conventions,
    match_series,
    scheduled_direction,
)

NOW = dt.datetime(2026, 7, 28, 12, 0, tzinfo=dt.UTC)


# ── Klasifikátor ───────────────────────────────────────────────────


def test_category_and_importance_from_text() -> None:
    fomc = classify("Fed holds rates steady after FOMC meeting")
    assert fomc.category == "FED"
    assert fomc.importance == 3

    cpi = classify("US CPI rises 0.3% in June")
    assert cpi.category == "MACRO_INFLATION"
    assert cpi.importance == 3

    geo = classify("Missile strike escalates conflict")
    assert geo.category == "GEOPOLITICS"
    assert geo.importance == 3

    boring = classify("Company announces new logo")
    assert boring.category == "OTHER"
    assert boring.importance == 1


def test_direction_from_phrases() -> None:
    assert classify_direction("Stocks surge as earnings beat estimates")[0] == 1
    assert classify_direction("Nasdaq plunges after guidance miss")[0] == -1
    # Geopolitická eskalace je risk-off bez ohledu na sloveso
    assert classify_direction("Russia launches missile attack")[0] == -1
    assert classify_direction("Company publishes annual report")[0] == 0


def test_mixed_signals_do_not_pretend_certainty() -> None:
    """Protichůdné signály: buď nula, nebo aspoň snížená síla."""
    balanced = classify_direction("Stocks fall as chipmakers beat estimates")
    assert balanced == (0, 0.0)

    leaning = classify("Stocks drop, slide and tumble though earnings beat")
    assert leaning.direction == -1
    assert leaning.strength < 0.4  # snížená proti jednoznačnému případu


def test_summary_helps_category_but_not_direction() -> None:
    """Směr se čte jen z titulku — v delším textu se slovesa vyruší."""
    result = classify("Update from the central bank", "The FOMC decided to hold rates")
    assert result.category == "FED"
    # Titulek sám o sobě směr nenese, i když shrnutí obsahuje „hold"
    assert result.direction == 0


def test_importance_ladder() -> None:
    assert classify_importance("Non-farm payrolls beat") == 3
    assert classify_importance("Quarterly earnings released") == 2
    assert classify_importance("Weather update") == 1


# ── Znaménkové konvence ────────────────────────────────────────────


def test_scheduled_direction_uses_series_convention() -> None:
    # Vyšší inflace než konsensus = risk-off
    assert scheduled_direction("USD CPI m/m", 1.5) == -1
    assert scheduled_direction("USD CPI m/m", -1.5) == 1
    # Silnější payrolls = risk-on
    assert scheduled_direction("USD Non-Farm Employment Change", 2.0) == 1
    # Vyšší nezaměstnanost = risk-off
    assert scheduled_direction("USD Unemployment Rate", 1.0) == -1
    # Překvapení přesně na konsensu směr nedává
    assert scheduled_direction("USD CPI m/m", 0.0) == 0
    # Neznámá řada nebo chybějící překvapení → None, ne tipování
    assert scheduled_direction("AUD Building Approvals", 1.0) is None
    assert scheduled_direction("USD CPI m/m", None) is None


def test_match_series_prefers_specific() -> None:
    assert match_series("USD Unemployment Rate") is not None
    assert match_series("USD Unemployment Rate").sign == -1  # type: ignore[union-attr]
    assert match_series("USD Non-Farm Employment Change").sign == 1  # type: ignore[union-attr]


def test_check_conventions_flags_series_that_stopped_working() -> None:
    """„Good news is bad news": konvence se může celé měsíce mýlit — musí se to poznat."""
    # Řada predikovala +1, ale trh šel 8× z 10 dolů
    wrong = [ConventionOutcome("zaměstnanost", 1, -5.0) for _ in range(8)]
    wrong += [ConventionOutcome("zaměstnanost", 1, 5.0) for _ in range(2)]
    ok = [ConventionOutcome("inflace", -1, -3.0) for _ in range(12)]

    checks = {c.series: c for c in check_conventions(wrong + ok)}
    assert checks["zaměstnanost"].suspicious
    assert checks["zaměstnanost"].hit_rate == pytest.approx(0.2)
    assert not checks["inflace"].suspicious
    assert checks["inflace"].hit_rate == pytest.approx(1.0)


def test_small_sample_is_not_flagged() -> None:
    """Tři minutí konvenci nevyvrátí — jinak by se flagovalo pořád."""
    few = [ConventionOutcome("růst", 1, -1.0) for _ in range(3)]
    assert not check_conventions(few)[0].suspicious


def test_outcomes_without_direction_are_ignored() -> None:
    assert check_conventions([ConventionOutcome("inflace", 0, 5.0)]) == []


# ── Job ────────────────────────────────────────────────────────────


def add_event(engine, **values) -> int:  # type: ignore[no-untyped-def]
    payload = {
        "ts_event": NOW,
        "ts_ingested": NOW,
        "source": "rss_news",
        "kind": "headline",
        "symbols": [],
        "market_closed": False,
        "raw": {},
    }
    payload.update(values)
    with engine.begin() as conn:
        key = conn.execute(insert(news_events).values(**payload)).inserted_primary_key
    assert key is not None
    return int(key[0])


def test_job_writes_version_one_and_denormalises(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    add_event(engine, title="Nasdaq plunges as chip guidance misses", dedup_hash="a")

    job = RuleClassificationJob(engine)
    assert job.run(NOW) == 1

    with engine.connect() as conn:
        cls = conn.execute(select(news_classifications)).fetchone()
        event = conn.execute(select(news_events)).fetchone()
    assert cls is not None and event is not None
    assert cls.version == 1
    assert cls.source == "rule"
    assert cls.direction == -1
    # Denormalizace do news_events pro rychlé čtení feedu
    # „guidance miss" je EARNINGS — specifičtější vzor vyhrává nad TECH
    assert event.category == "EARNINGS"
    assert event.sentiment_dir == -1
    assert event.sentiment_source == "rule"
    assert event.sentiment_score is not None and event.sentiment_score < 0

    # Dávka pro WS push (#335) nese celý řádek, ne jen kategorii — UI z ní
    # skládá feed bez dalšího dotazu
    assert len(job.last_batch) == 1
    pushed = job.last_batch[0]
    assert pushed["category"] == "EARNINGS"
    assert pushed["sentiment_dir"] == -1
    assert pushed["title"] == "Nasdaq plunges as chip guidance misses"
    assert isinstance(pushed["ts_event"], str)

    # Opakovaný běh nepřepisuje ani nepřidává druhou pravidlovou verzi
    assert job.run(NOW) == 0
    # …a nesmí pushnout starou dávku znovu
    assert job.last_batch == []


def test_scheduled_event_takes_direction_from_convention(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    add_event(
        engine,
        title="USD CPI m/m",
        kind="scheduled",
        source="forexfactory",
        surprise_z=2.0,
        dedup_hash="cpi",
    )

    RuleClassificationJob(engine).run(NOW)

    with engine.connect() as conn:
        event = conn.execute(select(news_events)).fetchone()
    assert event is not None
    # Titulek „USD CPI m/m" sám směr nenese; konvence + překvapení dají risk-off
    assert event.sentiment_dir == -1
    assert event.category == "MACRO_INFLATION"
