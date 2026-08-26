"""Testy WavesJob (#292): ukládání vln, potvrzený vs unconfirmed stav."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    sentiment_daily,
    sentiment_waves,
)
from gexlens_news.waves_job import WavesJob

# „Dnes" pro job — poslední den řady je průběžný, předchozí uzavřené
TODAY = dt.date(2026, 7, 29)
NOW = dt.datetime(2026, 7, 29, 14, 0, tzinfo=dt.UTC)


def make_db(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine


def seed_daily(engine: Engine, closes: list[float], *, include_today: float | None = None) -> None:
    """Uzavřené dny končí včerejškem; `include_today` přidá průběžný dnešek."""
    rows = []
    start = TODAY - dt.timedelta(days=len(closes))
    for index, close in enumerate(closes):
        day = start + dt.timedelta(days=index)
        rows.append(
            {
                "date": day,
                "symbol": "ES",
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "update_time": NOW,
            }
        )
    if include_today is not None:
        rows.append(
            {
                "date": TODAY,
                "symbol": "ES",
                "open": include_today,
                "high": include_today,
                "low": include_today,
                "close": include_today,
                "update_time": NOW,
            }
        )
    with engine.begin() as conn:
        conn.execute(insert(sentiment_daily), rows)


def test_waves_are_stored_and_replaced_idempotently(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    # Golden řada z test_sentwaves: RiskOn vlna hloubky 0.9 uzavřená nulou
    seed_daily(engine, [0.0] * 10 + [1.0, 1.0, 0.0])
    job = WavesJob(engine, symbol="ES")

    payload, changed = job.run(NOW)
    assert changed
    with engine.connect() as conn:
        stored = conn.execute(select(sentiment_waves)).fetchall()
    assert len(stored) == 1
    assert stored[0].direction == "RiskOn"
    assert float(stored[0].depth) == pytest.approx(0.9)
    assert stored[0].length_days == 2

    # Druhý běh: full-replace, žádná duplicita, stav beze změny
    payload2, changed2 = job.run(NOW)
    assert not changed2
    with engine.connect() as conn:
        assert len(conn.execute(select(sentiment_waves)).fetchall()) == 1


def test_confirmed_state_ignores_todays_provisional_close(tmp_path: Path) -> None:
    """Přechody se potvrzují na denním close (SPEC 5.6) — dnešek jen indikuje."""
    engine = make_db(tmp_path)
    # Uzavřené dny: nuly → Neutral. Dnešní průběžná 1.0 by stav překlopila.
    seed_daily(engine, [0.0] * 10, include_today=1.0)
    payload, _ = WavesJob(engine, symbol="ES").run(NOW)

    assert payload["state"] == "Neutral"
    assert payload["unconfirmed"] is True
    assert payload["unconfirmed_state"] == "RiskOn"
    assert payload["last_close"] == pytest.approx(1.0)


def test_confirmed_state_from_closed_days(tmp_path: Path) -> None:
    engine = make_db(tmp_path)
    # Uzavřené dny končí RiskOn podmínkou (bez historie práh 0 → potvrzeno)
    seed_daily(engine, [0.0] * 10 + [1.0])
    payload, _ = WavesJob(engine, symbol="ES").run(NOW)

    assert payload["state"] == "RiskOn"
    assert payload["unconfirmed"] is False
    assert payload["current_wave"] is not None
    assert payload["current_wave"]["end_date"] is None
    assert payload["ma5"] == pytest.approx(0.2)
    assert payload["ma10"] == pytest.approx(0.1)


def test_depth_z_ze_sigmy_dne_konce_vlny(tmp_path: Path) -> None:
    """#640: hloubka v σ škály — σ platná v den konce vlny, kauzálně;
    bez σ zůstává depth_z NULL (žádný default)."""
    engine = make_db(tmp_path)
    closes = [
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        -1.0,
        -2.0,
        -3.0,
        -2.5,
        -1.0,
        1.0,
        2.0,
        3.0,
    ]  # prettier-ignore
    seed_daily(engine, closes)
    # σ jen pro poslední dny řady — vlny končící dřív σ nemají → NULL
    start = TODAY - dt.timedelta(days=len(closes))
    from sqlalchemy import update

    with engine.begin() as conn:
        for index in range(12, len(closes)):
            conn.execute(
                update(sentiment_daily)
                .where(sentiment_daily.c.date == start + dt.timedelta(days=index))
                .values(sigma=2.0)
            )
    job = WavesJob(engine, symbol="ES")
    job.run(NOW)
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                sentiment_waves.c.depth,
                sentiment_waves.c.depth_z,
                sentiment_waves.c.series_variant,
                sentiment_waves.c.end_date,
            ).order_by(sentiment_waves.c.start_date)
        ).fetchall()
    assert rows, "vlny se musely detekovat"
    for row in rows:
        if row.depth_z is not None:
            # σ = 2.0 → depth_z je přesně polovina surové hloubky
            assert row.depth_z == pytest.approx(row.depth / 2.0)
            assert row.series_variant == "zscore_100"
        else:
            assert row.series_variant is None
    # Aspoň jedna vlna σ éry převod má (probíhající bere poslední známou σ)
    assert any(row.depth_z is not None for row in rows)
