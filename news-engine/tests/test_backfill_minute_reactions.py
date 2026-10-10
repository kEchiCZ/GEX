"""Doplnění minutové fáze reakcí po pozdějším backfillu barů (#1494).

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
    REACTION_DAILY_WINDOWS,
    ensure_sentiment_schema,
    news_events,
    news_reactions,
)

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_minute_reactions.py"
DAY = dt.date(2026, 7, 29)
EVENT_TS = dt.datetime(2026, 7, 29, 20, 5, tzinfo=dt.UTC)
NO_BARS_TS = dt.datetime(2026, 7, 15, 14, 0, tzinfo=dt.UTC)
NOW = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.UTC)
DAILY = REACTION_DAILY_WINDOWS[0]
COMPUTED = dt.datetime(2026, 8, 17, 15, 0, tzinfo=dt.UTC)


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("backfill_minute_reactions", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclassy skriptu (odložené anotace) hledají svůj modul v sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = load_script()


def write_bars(data_dir: Path, symbol: str, day: dt.date, *, drift_bp: float) -> None:
    """Den plochých barů; od `EVENT_TS` cena o `drift_bp` výš."""
    directory = data_dir / "derived" / symbol / "bars"
    directory.mkdir(parents=True, exist_ok=True)
    start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.UTC)
    rows = []
    for minute in range(24 * 60):
        ts = start + dt.timedelta(minutes=minute)
        price = 7000.0 * (1 + drift_bp / 10_000 * (1 if ts >= EVENT_TS else 0))
        bar = {"open": price, "high": price, "low": price, "close": price, "volume": 1.0}
        rows.append({"ts_min": ts, **bar})
    pq.write_table(pa.Table.from_pylist(rows), directory / f"{day.isoformat()}.parquet")


def add_event(engine: Engine, ts: dt.datetime, title: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                insert(news_events)
                .values(
                    ts_event=ts,
                    ts_ingested=ts,
                    source="alpaca",
                    kind="headline",
                    title=title,
                    importance=2,
                    category="EARNINGS",
                    symbols=[],
                    market_closed=False,
                    dedup_hash=title,
                    raw={},
                )
                .returning(news_events.c.id)
            ).scalar_one()
        )


def add_reaction(engine: Engine, event_id: int, symbol: str, **values: object) -> None:
    with engine.begin() as conn:
        conn.execute(insert(news_reactions).values(event_id=event_id, symbol=symbol, **values))


def daily_only(engine: Engine, event_id: int) -> None:
    """Stav po výpočtu bez minutových barů: jen denní fáze (#1494)."""
    for symbol in ("ES", "NQ"):
        add_reaction(
            engine,
            event_id,
            symbol,
            **{f"ret_{DAILY}": 12.5, f"range_{DAILY}": 30.0, "computed_at_daily": COMPUTED},
        )


def reaction(engine: Engine, event_id: int, symbol: str) -> Any:
    with engine.connect() as conn:
        return (
            conn.execute(
                select(news_reactions).where(
                    news_reactions.c.event_id == event_id, news_reactions.c.symbol == symbol
                )
            )
            .mappings()
            .one()
        )


def make_engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    return engine


def test_fills_minute_phase_after_bars_appear_and_is_idempotent(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    data = tmp_path / "data"
    (data / "derived").mkdir(parents=True)
    event_id = add_event(engine, EVENT_TS, "MSFT Q4 EPS beats")
    daily_only(engine, event_id)

    before = script.run(engine, data, apply=True, now=NOW)  # bary ještě nejsou
    assert sum(before.missing.values()) == 2
    assert sum(before.filled.values()) == 0
    assert reaction(engine, event_id, "ES")["computed_at_min"] is None

    write_bars(data, "ES", DAY, drift_bp=20.0)
    write_bars(data, "NQ", DAY, drift_bp=-10.0)
    dry = script.run(engine, data, apply=False, now=NOW)
    assert dict(dry.filled) == {("2026-07", "ES"): 1, ("2026-07", "NQ"): 1}
    assert reaction(engine, event_id, "ES")["computed_at_min"] is None  # dry-run nic nezapíše

    applied = script.run(engine, data, apply=True, now=NOW)
    assert applied.windows > 0
    es = reaction(engine, event_id, "ES")
    nq = reaction(engine, event_id, "NQ")
    assert es["ret_5"] == pytest.approx(20.0)
    assert nq["ret_5"] == pytest.approx(-10.0)
    assert es["computed_at_min"] is not None
    assert es[f"ret_{DAILY}"] == 12.5  # denní fáze zůstala
    assert es["computed_at_daily"] is not None

    again = script.run(engine, data, apply=True, now=NOW)
    assert sum(again.missing.values()) == 0
    assert again.windows == 0


def test_existing_minute_phase_is_not_overwritten(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    data = tmp_path / "data"
    write_bars(data, "ES", DAY, drift_bp=20.0)
    write_bars(data, "NQ", DAY, drift_bp=-10.0)
    event_id = add_event(engine, EVENT_TS, "META Q2 revenue")
    add_reaction(engine, event_id, "ES", ret_5=99.0, range_5=1.0, computed_at_min=COMPUTED)
    add_reaction(engine, event_id, "NQ", **{f"ret_{DAILY}": 1.0, "computed_at_daily": COMPUTED})

    report = script.run(engine, data, apply=True, now=NOW)
    assert dict(report.missing) == {("2026-07", "NQ"): 1}
    assert reaction(engine, event_id, "ES")["ret_5"] == 99.0
    assert reaction(engine, event_id, "NQ")["ret_5"] == pytest.approx(-10.0)


def test_candidates_skip_events_without_rows_recent_and_without_bars(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    data = tmp_path / "data"
    write_bars(data, "ES", DAY, drift_bp=5.0)
    write_bars(data, "NQ", DAY, drift_bp=5.0)
    add_event(engine, EVENT_TS, "bez řádku reakcí")  # patří běžnému jobu
    recent = add_event(engine, NOW - dt.timedelta(minutes=30), "čerstvý")
    daily_only(engine, recent)
    no_bars = add_event(engine, NO_BARS_TS, "den bez barů")
    daily_only(engine, no_bars)

    found = list(script.candidates(engine, NOW))
    assert [candidate.event_id for candidate in found] == [no_bars]
    assert found[0].missing == frozenset({"ES", "NQ"})
    report = script.run(engine, data, apply=True, now=NOW)
    assert dict(report.missing) == {("2026-07", "ES"): 1, ("2026-07", "NQ"): 1}
    assert sum(report.filled.values()) == 0
    assert "| 2026-07 | ES | 1 | 0 | 1 |" in report.render(apply=True)
    assert reaction(engine, no_bars, "ES")["computed_at_min"] is None


def test_cached_bars_evict_oldest(tmp_path: Path) -> None:
    data = tmp_path / "data"
    write_bars(data, "ES", DAY, drift_bp=0.0)
    bars = script.CachedBars(data, capacity=2)
    first = bars.load_day("ES", DAY)
    assert bars.load_day("ES", DAY) is first  # z cache
    bars.load_day("ES", DAY + dt.timedelta(days=1))
    bars.load_day("ES", DAY + dt.timedelta(days=2))
    assert ("ES", DAY) not in bars._cache
    assert len(first) == 24 * 60


def test_symbol_without_row_is_inserted_and_market_closed_corrected(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    data = tmp_path / "data"
    write_bars(data, "ES", DAY, drift_bp=20.0)
    write_bars(data, "NQ", DAY, drift_bp=-10.0)
    event_id = add_event(engine, EVENT_TS, "AAPL Q3 EPS")
    with engine.begin() as conn:  # zpráva chybně odhadnutá jako „zavřeno“ (#339)
        conn.execute(
            news_events.update().where(news_events.c.id == event_id).values(market_closed=True)
        )
    add_reaction(engine, event_id, "ES", ret_5=99.0, range_5=1.0, computed_at_min=COMPUTED)

    report = script.run(engine, data, apply=True, now=NOW)
    assert dict(report.filled) == {("2026-07", "NQ"): 1}
    assert reaction(engine, event_id, "NQ")["ret_5"] == pytest.approx(-10.0)  # INSERT
    assert reaction(engine, event_id, "ES")["ret_5"] == 99.0
    with engine.connect() as conn:
        closed = conn.execute(
            select(news_events.c.market_closed).where(news_events.c.id == event_id)
        ).scalar_one()
    assert closed is False  # oba symboly obchodovaly


def test_baseline_is_taken_for_event_trading_day(tmp_path: Path, monkeypatch: Any) -> None:
    engine = make_engine(tmp_path)
    data = tmp_path / "data"
    (data / "derived").mkdir(parents=True)
    # 20:05 UTC = 15:05 CT → seance 29. 7.; 23:30 UTC = 18:30 CT → už seance 30. 7.
    evening = dt.datetime(2026, 7, 29, 23, 30, tzinfo=dt.UTC)
    for ts in (EVENT_TS, evening):
        daily_only(engine, add_event(engine, ts, f"zpráva {ts.isoformat()}"))
    days: list[tuple[str, dt.date]] = []
    monkeypatch.setattr(
        script.ReactionJob,
        "baseline_for",
        lambda self, symbol, day, **kwargs: days.append((symbol, day)),
    )
    script.run(engine, data, apply=False, now=NOW)
    assert days == [
        ("ES", DAY),
        ("NQ", DAY),
        ("ES", DAY + dt.timedelta(days=1)),
        ("NQ", DAY + dt.timedelta(days=1)),
    ]
