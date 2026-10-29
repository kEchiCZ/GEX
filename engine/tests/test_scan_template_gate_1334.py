"""Průzkum kritérií brány šablon (#1334) — `scripts/scan_template_gate_1334.py`.

Skript žije ve `scripts/`, načítá se přes importlib (vzor test_measure_template_gate_1334).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

from gexlens_engine.compute.risk import RealizedSetup
from gexlens_engine.compute.setups import SetupParams

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
EVENT_TS = dt.datetime(2026, 10, 1, 15, 0, tzinfo=dt.UTC)  # čtvrtek, seance 1. 10.


def _load(name: str) -> Any:
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass skriptu hledá svůj modul v sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script() -> Any:
    _load("measure_template_gate_1334")  # scan z něj importuje vzorek a čisté R
    return _load("scan_template_gate_1334")


def _row(
    closed: dt.datetime,
    outcome_r: float,
    *,
    symbol: str = "ES",
    created: dt.datetime | None = None,
    no_created: bool = False,
) -> RealizedSetup:
    """Setup wall_bounce se stopem 2 b (v rozpočtu ES i NQ)."""
    return RealizedSetup(
        symbol=symbol,
        template="wall_bounce",
        status="closed_target" if outcome_r > 0 else "closed_stop",
        outcome_r=outcome_r,
        closed_ts=closed,
        tradeable=False,
        affordable=True,
        entry=7000.0,
        stop=6998.0,
        created_ts=None if no_created else created or closed - dt.timedelta(minutes=30),
    )


def _events(script: Any, rows: list[RealizedSetup], skipped: dict[str, int]) -> list[Any]:
    events: list[Any] = script.build_events(
        rows,
        SetupParams(),
        date_from=dt.date(2026, 10, 1),
        date_to=dt.date(2026, 10, 1),
        skipped=skipped,
    )
    return events


def _event(script: Any, history: list[float]) -> Any:
    """Event s historií buňky `history` (čisté R, vzestupně podle uzavření)."""
    closed = [EVENT_TS - dt.timedelta(minutes=len(history) - i) for i in range(len(history))]
    pairs = tuple(zip(closed, history, strict=True))
    return script.Event(
        template="wall_bounce",
        symbol="ES",
        created_ts=EVENT_TS,
        session=dt.date(2026, 10, 1),
        gross=1.0,
        net=0.75,
        cell_history=pairs,
        template_history=pairs,
    )


def test_history_is_point_in_time_and_pooled_across_symbols(script: Any) -> None:
    """Historie buňky = jen řádky téhož symbolu uzavřené před vznikem; šablona i z NQ."""
    es_before = [_row(EVENT_TS - dt.timedelta(hours=2, minutes=i), 1.0) for i in range(3)]
    nq_before = [
        _row(EVENT_TS - dt.timedelta(hours=3, minutes=i), -1.0, symbol="NQ") for i in range(2)
    ]
    event = _row(EVENT_TS + dt.timedelta(minutes=20), 1.0, created=EVENT_TS)
    es_after = _row(EVENT_TS + dt.timedelta(minutes=10), -1.0, created=EVENT_TS)
    skipped: dict[str, int] = defaultdict(int)
    events = _events(script, [*es_before, *nq_before, event, es_after], skipped)

    es_events = [e for e in events if e.symbol == "ES" and e.created_ts == EVENT_TS]
    assert len(es_events) == 2
    for es_event in es_events:
        assert len(es_event.cell_history) == 3  # vlastní ani pozdější ES řádek se nepočítá
        assert len(es_event.template_history) == 5  # + 2 NQ téže šablony
    assert not skipped


def test_rows_without_created_ts_are_counted(script: Any) -> None:
    """Řádek bez vzniku se nevynechá potichu: počítadlo ho přizná v hlavičce."""
    rows = [
        _row(EVENT_TS + dt.timedelta(minutes=5), 1.0, created=EVENT_TS),
        _row(EVENT_TS + dt.timedelta(minutes=6), 1.0, no_created=True),
    ]
    skipped: dict[str, int] = defaultdict(int)
    events = _events(script, rows, skipped)

    assert len(events) == 1
    assert dict(skipped) == {"bez created_ts": 1}
    lines = script.report(
        events, dt.date(2026, 10, 2), SetupParams(), params_label="test", skipped=dict(skipped)
    )
    assert "- Vynecháno: bez created_ts 1" in lines
    assert "- Parametry: test; okno 60 seancí, n ≥ 30." in lines


def test_last_trades_positive_needs_full_count(script: Any) -> None:
    """„Posledních N obchodů“: pod N obchody nepustí, rozhoduje Ø jen posledních N."""
    criterion = script.last_trades_positive(3)
    assert not criterion(_event(script, [1.0, 1.0]))
    assert criterion(_event(script, [-5.0, 1.0, 1.0, -0.5]))
    assert not criterion(_event(script, [5.0, -1.0, -1.0, 0.5]))


def test_family_takes_gate_window_and_minimum_from_params(script: Any) -> None:
    """LB a Ø kritéria berou minimum brány z parametrů, ne napevno 30."""
    event = _event(script, [0.5] * 12)
    loose = dict(script.family(60, 10))
    strict = dict(script.family(60, 30))
    assert loose["LB čistě > 0"](event)
    assert not strict["LB čistě > 0"](event)
    assert strict["Ø čistě > 0, n ≥ 10"](event)  # varianta s vlastním n se nemění
