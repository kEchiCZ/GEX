"""Retro přepočet reakcí podle #1502 (minutová i denní fáze) a přehodnocení predikcí.

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
from sqlalchemy import create_engine, insert, select, update
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
from gexlens_news.reactions import last_closed_before

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "remeasure_reactions_1502.py"
DAY = dt.date(2026, 7, 28)
#: Kalendářní dny barů od DAY: denní okno 10 obchodních dní musí jít uzavřít
DAYS = 17
MINUTE = dt.datetime(2026, 7, 28, 14, 30, tzinfo=dt.UTC)
NEWS = MINUTE + dt.timedelta(seconds=40)  # uprostřed minuty: základ = close baru 14:29
CUTOFF = dt.datetime(2026, 11, 13, 22, 5, tzinfo=dt.UTC)  # start news-engine s #1502
OLD = dt.datetime(2026, 8, 20, 4, 0, tzinfo=dt.UTC)  # měřeno dřívějším vzorcem
NOW = dt.datetime(2026, 11, 13, 22, 30, tzinfo=dt.UTC)
WINDOWS = (1, 5, 15, 60)
DAILY_WINDOWS = (1440, 2880, 7200, 14400)
OUTCOME_WINDOWS = (1, 5, 1440)


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("remeasure_reactions_1502", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclassy skriptu (odložené anotace) hledají svůj modul v sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = load_script()


def write_bars(
    data_dir: Path, symbol: str, *, drift_bp: float, skip: frozenset[dt.datetime] = frozenset()
) -> None:
    """Ploché bary `DAYS` dní bez minut `skip`; od 14:30 dne DAY (zpráva) o `drift_bp` výš."""
    directory = data_dir / "derived" / symbol / "bars"
    directory.mkdir(parents=True, exist_ok=True)
    for offset in range(DAYS):
        day = DAY + dt.timedelta(days=offset)
        start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.UTC)
        rows = []
        for minute in range(24 * 60):
            ts = start + dt.timedelta(minutes=minute)
            if ts in skip:
                continue
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
        pq.write_table(pa.Table.from_pylist(rows), directory / f"{day.isoformat()}.parquet")


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
    """Řádek obou fází, jak ho zapsal dřívější vzorec: základ po zprávě → ~0 bp."""
    values: dict[str, object] = {"event_id": event_id, "symbol": symbol}
    for window in WINDOWS:
        values |= {
            f"ret_{window}": 0.0,
            f"range_{window}": 2.0 / 7000.0 * 10_000,
            f"vol_z_{window}": None,
            f"cont_{window}": False,
        }
    for window in DAILY_WINDOWS:
        values |= {
            f"ret_{window}": 0.0,
            f"range_{window}": 2.0 / 7000.0 * 10_000,
            f"cont_{window}": False,
        }
    # ES má uložený režim; levels pro den už nejsou (retence 90 dní) — nesmí se ztratit
    regime = "positive" if symbol == "ES" else None
    return values | {
        "deferred_min": False,
        "regime_min": regime,
        "computed_at_min": OLD,
        "deferred_daily": False,
        "regime_daily": regime,
        "computed_at_daily": OLD,
    }


def build_env(
    tmp_path: Path, skip: frozenset[dt.datetime] = frozenset()
) -> tuple[Engine, Path, int]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    data_dir = tmp_path / "data"
    write_bars(data_dir, "ES", drift_bp=20.0, skip=skip)
    write_bars(data_dir, "NQ", drift_bp=0.0, skip=skip)
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
                    "window_min": window,
                    "realized_dir": 0,
                    "correct": False,
                    "computed_at": OLD,
                }
                for symbol in ("ES", "NQ")
                for window in OUTCOME_WINDOWS
            ],
        )
    return engine, data_dir, event_id


@pytest.fixture
def env(tmp_path: Path) -> tuple[Engine, Path, int]:
    return build_env(tmp_path)


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


def outcomes(engine: Engine) -> dict[tuple[str, int], tuple[int, bool]]:
    with engine.connect() as conn:
        rows = conn.execute(select(news_prediction_outcomes)).fetchall()
    return {
        (str(row.symbol), int(row.window_min)): (int(row.realized_dir), bool(row.correct))
        for row in rows
    }


def run(engine: Engine, data_dir: Path, *, apply: bool) -> Any:
    return script.run(
        engine, data_dir, cutoff=CUTOFF, apply=apply, now=NOW, check_every=1, log=lambda _: None
    )


UNCHANGED = {(symbol, window): (0, False) for symbol in ("ES", "NQ") for window in OUTCOME_WINDOWS}


def test_dry_run_jen_spocita(env: tuple[Engine, Path, int]) -> None:
    engine, data_dir, event_id = env
    report = run(engine, data_dir, apply=False)
    assert sum(report.events.values()) == 1
    assert report._total(report.rows, "min") == 2 and report._total(report.changed, "min") == 2
    assert report._total(report.flipped, "min") == 1  # ES 0 → +20 bp; NQ zůstává 0
    assert sum(report.cont_5.values()) == 2  # event mezi základem a zprávou
    # Denní fáze: ES 0 → +20 bp; NQ na plochých barech beze změny
    assert report._total(report.rows, "daily") == 2
    assert report._total(report.changed, "daily") == 1
    assert report._total(report.flipped, "daily") == 1
    assert report.checked == 1  # úzký výřez = load_range v obou fázích
    assert dict(report.outcomes_flipped) == {1: 1, 5: 1, 1440: 1}
    assert report.outcomes_written == 0
    assert reaction(engine, event_id, "ES")["ret_5"] == 0.0
    assert reaction(engine, event_id, "ES")["ret_1440"] == 0.0
    assert outcomes(engine) == UNCHANGED


def test_apply_premeri_produkcni_cestou_a_je_idempotentni(env: tuple[Engine, Path, int]) -> None:
    engine, data_dir, event_id = env
    report = run(engine, data_dir, apply=True)
    assert report._total(report.rows, "min") == 2 and report._total(report.rows, "daily") == 2
    assert report.outcomes_written == 3 and report.sweep_written == 0

    # Totéž, co by změřil job s #1502 nad týmiž daty
    job = ReactionJob(engine, BarsRepository(data_dir))
    expected = job.measure_minute(NEWS, "TECH", {"ES": None, "NQ": None}, NOW).rows
    daily = job.measure_daily(
        NEWS, {symbol: job.daily_sessions(symbol) for symbol in ("ES", "NQ")}, NOW
    )
    assert daily is not None
    for row in [*expected, *daily]:
        stored = reaction(engine, event_id, row.symbol)
        for key, value in row.values.items():
            if key.startswith(("computed_at_", "regime_")):
                continue
            if isinstance(value, float):
                assert stored[key] == pytest.approx(value), key
            else:
                assert stored[key] == value, key
    es = reaction(engine, event_id, "ES")
    assert es["ret_1"] == pytest.approx(20.0) and es["ret_1440"] == pytest.approx(20.0)
    assert all(es[f"cont_{window}"] for window in WINDOWS)
    assert not any(es[f"cont_{window}"] for window in DAILY_WINDOWS)
    assert outcomes(engine) == UNCHANGED | {("ES", w): (1, True) for w in OUTCOME_WINDOWS}
    # Uložený režim mimo retenci levels zůstává v obou fázích
    assert es["regime_min"] == "positive" and es["regime_daily"] == "positive"
    assert report.regime_kept == {"min": 1, "daily": 1}
    nq = reaction(engine, event_id, "NQ")
    assert nq["regime_min"] is None and nq["regime_daily"] is None

    again = run(engine, data_dir, apply=True)
    assert sum(again.events.values()) == 0 and again.outcomes_written == 0
    assert again.sweep_seen == 6 and again.sweep_written == 0


def test_premeri_jen_fazi_zmerenou_pred_cutoff(env: tuple[Engine, Path, int]) -> None:
    """Minutová fáze už je z nového kódu (≥ cutoff): retro přeměří jen denní."""
    engine, data_dir, event_id = env
    with engine.begin() as conn:
        conn.execute(update(news_reactions).values(computed_at_min=CUTOFF))
    report = run(engine, data_dir, apply=True)
    assert report._total(report.rows, "min") == 0 and report._total(report.rows, "daily") == 2
    es = reaction(engine, event_id, "ES")
    assert es["ret_5"] == 0.0 and es["ret_1440"] == pytest.approx(20.0)
    # Outcomes minutových oken čtou výnos řádku, který je už z nového kódu (zde 0 bp)
    assert outcomes(engine) == UNCHANGED | {("ES", 1440): (1, True)}


def test_zaverecna_kontrola_opravi_prerusenou_davku(env: tuple[Engine, Path, int]) -> None:
    """Reakce zapsané, outcomes ne (pád mezi zápisy): běh „hotovo“ je dorovná podle stavu."""
    engine, data_dir, _ = env
    run(engine, data_dir, apply=True)
    with engine.begin() as conn:
        conn.execute(
            update(news_prediction_outcomes)
            .where(news_prediction_outcomes.c.symbol == "ES")
            .values(realized_dir=0, correct=False)
        )
    report = run(engine, data_dir, apply=True)
    assert sum(report.events.values()) == 0 and report.sweep_written == 3
    assert outcomes(engine) == UNCHANGED | {("ES", w): (1, True) for w in OUTCOME_WINDOWS}


def test_cutoff_v_budoucnosti_skonci(env: tuple[Engine, Path, int]) -> None:
    engine, data_dir, _ = env
    with pytest.raises(SystemExit):
        script.run(engine, data_dir, cutoff=NOW + dt.timedelta(hours=1), apply=False, now=NOW)


def test_uzky_vyrez_odmitne_jiny_rozsah(env: tuple[Engine, Path, int]) -> None:
    _, data_dir, _ = env
    narrow = script.NarrowBars(script.CachedBars(data_dir))
    with pytest.raises(ValueError):
        narrow.load_range("ES", NEWS, NEWS + dt.timedelta(hours=1))


def test_denni_vyrez_dava_totez_co_load_range(env: tuple[Engine, Path, int]) -> None:
    """Zpráva v celou minutu i uprostřed: základ a první bar z výřezu = z plného rozsahu."""
    _, data_dir, _ = env
    cached = script.CachedBars(data_dir)
    narrow = script.NarrowBars(cached)
    lookback, lookahead = script.LOOKBACK, script.LOOKAHEAD
    for t in (MINUTE, NEWS, MINUTE + dt.timedelta(days=3, hours=5, seconds=1)):
        sliced = narrow.load_range("ES", t - lookback, t + lookahead)
        full = cached.load_range("ES", t - lookback, t + lookahead)
        assert last_closed_before(sliced, t) == last_closed_before(full, t)
        first = next(bar for bar in full if bar.ts >= t)
        assert next(bar for bar in sliced if bar.ts >= t) == first


def test_okno_bez_baru_noveho_useku_dostane_null(tmp_path: Path) -> None:
    """Chybí bar 14:30: okno 1 (bary 14:30) nejde změřit, uložená hodnota nesmí zůstat."""
    engine, data_dir, event_id = build_env(tmp_path, skip=frozenset({MINUTE}))
    dry = run(engine, data_dir, apply=False)
    assert dry.vanished["min"] == 2 and dry.outcomes_orphaned == 2

    report = run(engine, data_dir, apply=True)
    assert report.vanished["min"] == 2 and report.outcomes_orphaned == 2
    assert report.sweep_orphaned == 2
    es = reaction(engine, event_id, "ES")
    assert all(es[f"{column}_1"] is None for column in ("ret", "range", "vol_z", "cont"))
    assert es["ret_5"] == pytest.approx(20.0)  # okno 5 má bary 14:31–14:34
    # Outcome okna bez výnosu zůstává beze změny; okno 5 a 1 den se přehodnotí
    assert outcomes(engine) == UNCHANGED | {("ES", 5): (1, True), ("ES", 1440): (1, True)}


def test_radek_jen_s_denni_fazi_minutovou_nemeni(env: tuple[Engine, Path, int]) -> None:
    """Řádek bez minutové fáze: retro přeměří jen denní a `market_closed` nechá být."""
    engine, data_dir, event_id = env
    minute_columns = {
        f"{column}_{window}": None
        for window in WINDOWS
        for column in ("ret", "range", "vol_z", "cont")
    }
    with engine.begin() as conn:
        conn.execute(
            update(news_reactions).values(
                **minute_columns, deferred_min=None, regime_min=None, computed_at_min=None
            )
        )
        conn.execute(
            update(news_events).where(news_events.c.id == event_id).values(market_closed=True)
        )
    report = run(engine, data_dir, apply=True)
    assert report._total(report.rows, "min") == 0 and report._total(report.rows, "daily") == 2
    es = reaction(engine, event_id, "ES")
    assert all(es[key] is None for key in minute_columns) and es["computed_at_min"] is None
    assert es["ret_1440"] == pytest.approx(20.0)
    with engine.connect() as conn:
        closed = conn.execute(
            select(news_events.c.market_closed).where(news_events.c.id == event_id)
        ).scalar_one()
    assert closed is True


def test_nesoulad_vyrezu_s_load_range_skonci(
    env: tuple[Engine, Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, data_dir, event_id = env
    original = script.DayWindow.minute_slice
    monkeypatch.setattr(script.DayWindow, "minute_slice", lambda self, t: original(self, t)[3:])
    with pytest.raises(SystemExit, match="Úzký výřez"):
        run(engine, data_dir, apply=True)
    # Nic se nezapsalo: řádek je dál z dřívějšího vzorce
    assert reaction(engine, event_id, "ES")["ret_5"] == 0.0


def test_zaverecna_kontrola_po_castech_pokracuje_od_predikce(
    env: tuple[Engine, Path, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, data_dir, event_id = env
    with engine.begin() as conn:
        second = conn.execute(
            insert(news_predictions).values(
                event_id=event_id, predicted_dir=-1, predictor="llm", created_at=OLD
            )
        ).inserted_primary_key
        assert second is not None
        conn.execute(
            insert(news_prediction_outcomes),
            [
                {
                    "prediction_id": int(second[0]),
                    "symbol": "ES",
                    "window_min": window,
                    "realized_dir": 1,
                    "correct": False,
                    "computed_at": OLD,
                }
                for window in OUTCOME_WINDOWS
            ],
        )
    run(engine, data_dir, apply=True)
    with engine.begin() as conn:
        conn.execute(update(news_prediction_outcomes).values(realized_dir=0, correct=False))
    monkeypatch.setattr(script, "SWEEP_PREDICTIONS", 1)
    first = script.Report()
    calls = iter([None, "časový limit 9 min"])
    script.sweep_outcomes(
        engine, CUTOFF, first, batch=10, start=None, limit_reason=lambda: next(calls)
    )
    assert first.sweep_written == 3 and first.sweep_resume == int(second[0])
    assert "--sweep-from" in first.stopped
    rest = script.Report()
    script.sweep_outcomes(
        engine, CUTOFF, rest, batch=10, start=first.sweep_resume, limit_reason=lambda: None
    )
    assert rest.sweep_written == 3 and rest.sweep_resume is None
    with engine.connect() as conn:
        llm = conn.execute(
            select(news_prediction_outcomes).where(
                news_prediction_outcomes.c.prediction_id == int(second[0])
            )
        ).fetchall()
    # Predikce dolů, ES vzrostl o 20 bp: realizováno nahoru, nesprávně
    assert {(int(row.realized_dir), bool(row.correct)) for row in llm} == {(1, False)}
