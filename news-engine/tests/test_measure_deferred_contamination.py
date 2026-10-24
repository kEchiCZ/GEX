"""Varianty kontaminace K1 odložené reakce pro rozhodnutí #1659.

Skript žije ve `scripts/`, načítá se přes importlib jako ostatní skripty.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from gexlens_news.contamination import Contaminators
from gexlens_news.reactions import window_contaminated

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "measure_deferred_contamination.py"
# Zpráva v pátek po zavření, okna od nedělního otevření Globexu
NEWS = dt.datetime(2026, 10, 23, 21, 4, tzinfo=dt.UTC)
OPEN = dt.datetime(2026, 10, 25, 22, 0, tzinfo=dt.UTC)


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("measure_deferred_contamination", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("other", "category", "expected"),
    [
        # Do 61 min po zprávě kontaminuje ve všech variantách
        (NEWS + dt.timedelta(minutes=30), "FED", {"C": True, "A": True, "B": True}),
        # Sobota v uzavírce: jen celá uzavírka (A)
        (
            dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.UTC),
            "FED",
            {"C": False, "A": True, "B": False},
        ),
        # Těsně po otevření v okně 60: A i B, dnešní stav (C) ne
        (OPEN + dt.timedelta(minutes=10), "FED", {"C": False, "A": True, "B": True}),
        # Přesně na otevření: okno [začátek, začátek + okno) ho obsahuje
        (OPEN, "FED", {"C": False, "A": True, "B": True}),
        # Za koncem okna ani stejná kategorie nekontaminují nikde
        (OPEN + dt.timedelta(minutes=61), "FED", {"C": False, "A": False, "B": False}),
        (OPEN + dt.timedelta(minutes=10), "MACRO", {"C": False, "A": False, "B": False}),
    ],
)
def test_varianty_k1_odlozene_reakce(
    other: dt.datetime, category: str, expected: dict[str, bool]
) -> None:
    script = load_script()
    contaminators = Contaminators.build([(NEWS, "MACRO", 3), (other, category, 2)])
    assert script.variant_flags(contaminators, "MACRO", NEWS, OPEN, 60) == expected


def test_c_odpovida_predikatu_jobu() -> None:
    """C je dnešní `window_contaminated` nad eventy z `Contaminators.around`."""
    script = load_script()
    others = [NEWS + dt.timedelta(minutes=m) for m in (0, 1, 30, 61, 62)]
    contaminators = Contaminators.build([(ts, "FED", 2) for ts in others])
    for start in (NEWS, NEWS + dt.timedelta(minutes=20), OPEN):
        for window in (1, 5, 15, 60):
            expected = window_contaminated(NEWS, start, window, contaminators.around(NEWS, "MACRO"))
            flags = script.variant_flags(contaminators, "MACRO", NEWS, start, window)
            assert flags["C"] is expected
