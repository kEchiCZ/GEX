"""Retro přepočet minutové fáze reakcí podle #1502 a přehodnocení predikcí okna 5.

Skript žije ve `scripts/`, načítá se přes importlib jako ostatní skripty.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from gexlens_engine.storage.sentiment import (
    ensure_sentiment_schema,
    news_events,
    news_prediction_outcomes,
    news_predictions,
    news_reactions,
)
from gexlens_news.bars import BarsRepository
from gexlens_news.reaction_job import ReactionJob

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "remeasure_reactions_1502.py"
DAY = dt.date(2026, 7, 28)
MINUTE = dt.datetime(2026, 7, 28, 14, 30, tzinfo=dt.UTC)
NEWS = MINUTE + dt.timedelta(seconds=40)  # uprostřed minuty: základ = close baru 14:29
CUTOFF = dt.datetime(2026, 11, 13, 22, 5, tzinfo=dt.UTC)  # start news-engine s #1502
OLD = dt.datetime(2026, 7, 28, 15, 35, tzinfo=dt.UTC)  # měřeno dřívějším vzorcem
NOW = dt.datetime(2026, 11, 13, 22, 30, tzinfo=dt.UTC)
WINDOWS = (1, 5, 15, 60)


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("remeasure_reactions_1502", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclassy skriptu (odložené anotace) hledají svůj modul v sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = load_script()


def write_bars(data_dir: Path, symbol: str, *, drift_bp: float) -> None:
    """Den plochých barů; od baru 14:30 (minuta zprávy) cena o `drift_bp` výš."""
    directory = data_dir / "derived" / symbol / "bars"
    directory.mkdir(parents=True, exist_ok=True)
    start = dt.datetime.combine(DAY, dt.time(0, 0), tzinfo=dt.UTC)
    rows = []
    for minute in range(24 * 60):
        ts = start + dt.timedelta(minutes=minute)
        price = 7000.0 * (1 + drift_bp / 10_000 * (1 if ts >= MINUTE else 0))
        rows.append(
            {
                "ts_min": ts,
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": 100.0,
            }
        )
    pq.write_table(pa.Table.from_pylist(rows), directory / f"{DAY.isoformat()}.parquet")


def add_event(
    engine: Engine, ts: dt.datetime, *, importance: int, category: str, title: str
) -> int:
    with engine.begin() as conn:
        key = conn.execute(
            insert(news_events).values(
                ts_event=ts,
                ts_ingested=ts,
                source="finnhub",
                kind="headline",
                title=title,
                importance=importance,
                category=category,
                symbols=[],
                market_closed=False,
                dedup_hash=title,
                raw={},
            )
        ).inserted_primary_key
    assert key is not None
    return int(key[0])


def old_row(event_id: int, symbol: str) -> dict[str, object]:
    """Řádek minutové fáze, jak ho zapsal dřívější vzorec: základ po zprávě → ~0 bp."""
    values: dict[str, object] = {"event_id": event_id, "symbol": symbol}
    for window in WINDOWS:
        values |= {
            f"ret_{window}": 0.0,
            f"range_{window}": 2.0 / 7000.0 * 10_000,
            f"vol_z_{window}": None,
            f"cont_{window}": False,
        }
    return values | {"deferred_min": False, "regime_min": None, "computed_at_min": OLD}


@pytest.fixture
def env(tmp_path: Path) -> tuple[Engine, Path, int]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    data_dir = tmp_path / "data"
    write_bars(data_dir, "ES", drift_bp=20.0)
    write_bars(data_dir, "NQ", drift_bp=0.0)
    event_id = add_event(engine, NEWS, importance=1, category="TECH", title="Zpráva")
    # Mezi close základu (14:30:00) a zprávou — kontaminuje podle (a)
    add_event(engine, MINUTE + dt.timedelta(seconds=20), importance=2, category="FED", title="Jiná")
    with engine.begin() as conn:
        conn.execute(insert(news_reactions), [old_row(event_id, "ES"), old_row(event_id, "NQ")])
        prediction = conn.execute(
            insert(news_predictions).values(
                event_id=event_id, predicted_dir=1, predictor="rule", created_at=OLD
            )
        ).inserted_primary_key
        assert prediction is not None
        conn.execute(
            insert(news_prediction_outcomes),
            [
                {
                    "prediction_id": int(prediction[0]),
                    "symbol": symbol,
                    "window_min": 5,
                    "realized_dir": 0,
                    "correct": False,
                    "computed_at": OLD,
                }
                for symbol in ("ES", "NQ")
            ],
        )
    return engine, data_dir, event_id


def reaction(engine: Engine, event_id: int, symbol: str) -> dict[str, Any]:
    with engine.connect() as conn:
        row = (
            conn.execute(
                select(news_reactions).where(
                    news_reactions.c.event_id == event_id, news_reactions.c.symbol == symbol
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


def outcomes(engine: Engine) -> dict[str, tuple[int, bool]]:
    with engine.connect() as conn:
        rows = conn.execute(select(news_prediction_outcomes)).fetchall()
    return {str(row.symbol): (int(row.realized_dir), bool(row.correct)) for row in rows}


def run(engine: Engine, data_dir: Path, *, apply: bool) -> Any:
    return script.run(
        engine, data_dir, cutoff=CUTOFF, apply=apply, now=NOW, check_every=1, log=lambda _: None
    )


def test_dry_run_jen_spocita(env: tuple[Engine, Path, int]) -> None:
    engine, data_dir, event_id = env
    report = run(engine, data_dir, apply=False)
    assert sum(report.events.values()) == 1
    assert sum(report.rows.values()) == 2 and sum(report.changed.values()) == 2
    assert sum(report.flipped_5.values()) == 1  # ES 0 → +20 bp; NQ zůstává 0
    assert sum(report.cont_5.values()) == 2  # event mezi základem a zprávou
    assert report.checked == 1  # úzký výřez = load_range
    assert report.outcomes_flipped == 1 and report.outcomes_written == 0
    assert reaction(engine, event_id, "ES")["ret_5"] == 0.0
    assert outcomes(engine) == {"ES": (0, False), "NQ": (0, False)}


def test_apply_premeri_produkcni_cestou_a_je_idempotentni(
    env: tuple[Engine, Path, int], tmp_path: Path
) -> None:
    engine, data_dir, event_id = env
    report = run(engine, data_dir, apply=True)
    assert sum(report.rows.values()) == 2 and report.outcomes_written == 1

    # Totéž, co by změřil job s #1502 nad týmiž daty
    expected = ReactionJob(engine, BarsRepository(data_dir)).measure_minute(
        NEWS, "TECH", {"ES": None, "NQ": None}, NOW
    )
    for row in expected.rows:
        stored = reaction(engine, event_id, row.symbol)
        for key, value in row.values.items():
            if key == "computed_at_min":
                continue
            if isinstance(value, float):
                assert stored[key] == pytest.approx(value), key
            else:
                assert stored[key] == value, key
    es = reaction(engine, event_id, "ES")
    assert es["ret_1"] == pytest.approx(20.0)
    assert all(es[f"cont_{window}"] for window in WINDOWS)
    assert outcomes(engine) == {"ES": (1, True), "NQ": (0, False)}

    again = run(engine, data_dir, apply=True)
    assert sum(again.events.values()) == 0 and again.outcomes_written == 0


def test_cutoff_v_budoucnosti_skonci(env: tuple[Engine, Path, int]) -> None:
    engine, data_dir, _ = env
    with pytest.raises(SystemExit):
        script.run(engine, data_dir, cutoff=NOW + dt.timedelta(hours=1), apply=False, now=NOW)


def test_uzky_vyrez_odmitne_jiny_rozsah(env: tuple[Engine, Path, int]) -> None:
    _, data_dir, _ = env
    narrow = script.NarrowBars(script.CachedBars(data_dir))
    with pytest.raises(ValueError):
        narrow.load_range("ES", NEWS, NEWS + dt.timedelta(hours=1))
