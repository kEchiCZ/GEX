"""Přehrání v5 × v6 — párování setupů a report (#1590, ADR-0057 bod 7).

Skript žije ve `scripts/`, načítá se přes importlib (vzor test_backtest_replay).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "compare_mechanics_v5_v6", SCRIPTS / "compare_mechanics_v5_v6.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cmp = _load()
CREATED = dt.datetime(2026, 10, 14, 14, 0, tzinfo=dt.UTC)


def _setup(minutes: int, r: float | None, direction: str = "long") -> dict[str, Any]:
    return {
        "template": "wall_bounce",
        "direction": direction,
        "created": CREATED + dt.timedelta(minutes=minutes),
        "r": r,
    }


def test_setup_se_paruje_sablonou_smerem_a_minutou_vzniku() -> None:
    rows5 = cmp.keyed([_setup(0, 1.0), _setup(1, -1.0)])
    rows6 = cmp.keyed([_setup(0, 1.0), _setup(1, 2.0, "short"), _setup(5, None)])
    tally = cmp.Tally()
    tally.add(rows5, rows6)
    # shodný je jen setup 14:00 long; 14:01 short je jiný setup než 14:01 long
    assert (tally.v5, tally.v6, tally.both) == (2, 3, 1)
    assert tally.r_v5 == 0.0
    assert tally.r_v6 == 3.0  # otevřený setup (r None) do ΣR nejde


def test_report_nese_pokryti_a_radek_sablony() -> None:
    tally = cmp.Tally(v5=2, v6=3, both=1, r_v5=0.0, r_v6=3.0)
    text = cmp.render(
        {"produkční parametry": {"ES": {"wall_bounce": tally}}},
        {"ES": (1, 400, 4)},
        dt.date(2026, 9, 21),
        dt.date(2026, 10, 15),
        "defaulty kódu",
    )
    assert "| ES | 1 | 400 | 4 (1.0%) |" in text
    assert "| ES | wall_bounce | 2 | 3 | 1 | 1 | 2 | +0.00 | +3.00 | +3.00 |" in text
    assert "### produkční parametry — baseline defaulty kódu" in text
