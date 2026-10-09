"""Předregistrované měření výjimky mega caps (ADR-0059 bod 4, #1491).

Skript žije ve `scripts/`, načítá se přes importlib jako ostatní skripty.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, insert

from gexlens_engine.storage.sentiment import ensure_sentiment_schema, news_events, news_reactions

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "measure_megacap_earnings.py"
T0 = dt.datetime(2026, 8, 3, 20, 5, 10, tzinfo=dt.UTC)


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("measure_megacap_earnings", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclassy skriptu (odložené anotace) hledají svůj modul v sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = load_script()


def reaction(ts: dt.datetime, symbols: set[str], range_bp: float, ret_bp: float = 0.0) -> Any:
    return script.Reaction(ts, frozenset(symbols), range_bp, ret_bp)


def test_split_headlines_by_mega_cap_symbol() -> None:
    rows = [
        reaction(T0, {"NVDA"}, 30.0),
        reaction(T0, {"NVDA", "XYZ"}, 31.0),
        reaction(T0, {"XYZ"}, 5.0),
        reaction(T0, set(), 6.0),
    ]
    mega, other = script.split_headlines(rows, lambda row: row.range_bp)
    assert mega == [30.0, 31.0]
    assert other == [5.0, 6.0]


def test_split_windows_minute_with_mega_cap_goes_only_to_mega() -> None:
    same_minute = T0.replace(second=40)
    rows = [
        reaction(T0, {"AAPL"}, 20.0),
        reaction(same_minute, {"XYZ"}, 22.0),  # tytéž bary jako AAPL → jen mega
        reaction(T0 + dt.timedelta(minutes=1), {"XYZ"}, 4.0),
        reaction(T0 + dt.timedelta(minutes=1, seconds=30), {"ABC"}, 6.0),
        reaction(T0 + dt.timedelta(minutes=7), {"ABC"}, 3.0),
    ]
    mega, other = script.split_windows(rows, lambda row: row.range_bp)
    assert mega == [21.0]
    assert sorted(other) == [3.0, 5.0]


def test_bootstrap_separated_groups_pass() -> None:
    mega = [20.0 + i % 5 for i in range(40)]
    other = [5.0 + i % 5 for i in range(200)]
    comparison = script.bootstrap_median_diff(mega, other, resamples=2000)
    assert comparison.diff == 15.0
    assert 0 < comparison.ci_low <= comparison.diff <= comparison.ci_high
    assert comparison.passes


def test_bootstrap_same_distribution_ci_spans_zero() -> None:
    values = [float(i % 17) for i in range(120)]
    comparison = script.bootstrap_median_diff(values[:60], values[60:], resamples=2000)
    assert comparison.ci_low <= 0 <= comparison.ci_high
    assert not comparison.passes


def test_bootstrap_is_deterministic_with_seed() -> None:
    mega = [float(i) for i in range(35)]
    other = [float(i) / 2 for i in range(90)]
    first = script.bootstrap_median_diff(mega, other, resamples=1000)
    second = script.bootstrap_median_diff(mega, other, resamples=1000)
    assert first == second


def test_small_group_fails_even_with_positive_ci() -> None:
    comparison = script.bootstrap_median_diff([30.0] * 29, [5.0] * 100, resamples=500)
    assert comparison.ci_low > 0
    assert not comparison.passes  # n mega 29 < 30


def test_empty_group_does_not_crash() -> None:
    comparison = script.bootstrap_median_diff([], [5.0, 6.0])
    assert comparison.n_mega == 0
    assert not comparison.passes


def test_verdict_requires_all_units() -> None:
    passing = script.Comparison(40, 100, 20.0, 5.0, 15.0, 10.0, 20.0)
    failing = script.Comparison(40, 100, 6.0, 5.0, 1.0, -1.0, 3.0)
    assert "splněno" in script.verdict([passing, passing])
    assert "nesplněno" in script.verdict([failing, failing])
    assert "neshoda" in script.verdict([passing, failing])


def test_load_reactions_filters(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'news.sqlite'}")
    ensure_sentiment_schema(engine)
    live = script.LIVE_FROM + dt.timedelta(days=5)
    old = script.LIVE_FROM - dt.timedelta(days=5)
    # (ts, kategorie, symbol reakce, cont_5, deferred, range_5) → má projít?
    cases = [
        (live, "EARNINGS", "NQ", False, False, 12.0, True),
        (old, "EARNINGS", "NQ", False, None, 13.0, True),  # jen bez `since`
        (live, "TECH", "NQ", False, False, 14.0, False),
        (live, "EARNINGS", "ES", False, False, 15.0, False),
        (live, "EARNINGS", "NQ", True, False, 16.0, False),  # kontaminace
        (live, "EARNINGS", "NQ", None, False, 17.0, False),  # okno bez baru
        (live, "EARNINGS", "NQ", False, True, 18.0, False),  # odložená reakce
        (live, "EARNINGS", "NQ", False, False, None, False),
    ]
    with engine.begin() as conn:
        for index, (ts, category, symbol, cont, deferred, range_bp, _) in enumerate(cases):
            event_id = conn.execute(
                insert(news_events)
                .values(
                    ts_event=ts,
                    ts_ingested=ts,
                    source="alpaca",
                    kind="headline",
                    category=category,
                    title=f"zpráva {index}",
                    symbols=["NVDA"],
                    market_closed=False,
                    dedup_hash=f"h{index}",
                    raw={},
                )
                .returning(news_events.c.id)
            ).scalar_one()
            conn.execute(
                insert(news_reactions).values(
                    event_id=event_id,
                    symbol=symbol,
                    ret_5=-3.0,
                    range_5=range_bp,
                    cont_5=cont,
                    deferred_min=deferred,
                )
            )
    live_rows = script.load_reactions(engine, since=script.LIVE_FROM)
    assert [row.range_bp for row in live_rows] == [12.0]
    assert live_rows[0].symbols == frozenset({"NVDA"})
    assert live_rows[0].ts_event.tzinfo is not None
    assert sorted(row.range_bp for row in script.load_reactions(engine, since=None)) == [
        12.0,
        13.0,
    ]
