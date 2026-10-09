"""Verdikt sondy market data lines (#1477) nad syntetickými fázemi A/B/A.

Skript žije ve `scripts/`, načítá se přes importlib jako u ostatních skriptů
s testem. Spouštěcí podnět: při zavřeném trhu nic neteče, a verdikt pak bez
pojistky hlásil „pásky berou 0 lines, strop 0“.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "lines_probe.py"


@pytest.fixture(scope="module")
def mod() -> Any:
    spec = importlib.util.spec_from_file_location("lines_probe", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["lines_probe"] = module
    spec.loader.exec_module(module)
    return module


def fill(mod: Any, label: str, streaming: int, subscribed: int = 250) -> Any:
    return mod.Fill(label, subscribed, subscribed, streaming)


def test_no_stream_is_undecided(mod: Any) -> None:
    a, b, a2 = (fill(mod, name, 0) for name in ("A", "B", "A2"))
    assert mod.verdict(a, b, a2, 2).startswith("NEROZHODNUTO: žádný proud")


def test_cap_not_reached_is_undecided(mod: Any) -> None:
    a, b, a2 = (fill(mod, name, 250) for name in ("A", "B", "A2"))
    assert "strop nedosažen" in mod.verdict(a, b, a2, 2)


def test_drift_between_a_and_a2_is_undecided(mod: Any) -> None:
    result = mod.verdict(fill(mod, "A", 100), fill(mod, "B", 98), fill(mod, "A2", 97), 2)
    assert result.startswith("NEROZHODNUTO: proud bez pásek")


def test_tapes_use_lines(mod: Any) -> None:
    result = mod.verdict(fill(mod, "A", 100), fill(mod, "B", 98), fill(mod, "A2", 100), 2)
    assert result.startswith("NEWS pásky (2) berou 2 market data lines; strop bez pásek 100")


def test_ranges(mod: Any) -> None:
    assert mod._ranges([]) == "—"
    assert mod._ranges([1, 2, 3, 7]) == "1–3, 7"
    assert mod._ranges([5]) == "5"
