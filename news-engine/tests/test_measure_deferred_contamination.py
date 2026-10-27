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


PAUSE_NEWS = dt.datetime(2026, 10, 22, 21, 30, tzinfo=dt.UTC)
PAUSE_OPEN = dt.datetime(2026, 10, 22, 22, 0, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("news", "start", "other", "window", "expected"),
    [
        # Zpráva v denní pauze, jiná po konci okna 15, ale do 61 min po zprávě:
        # výnos okna neovlivnila — žádná varianta (kontrola PR #1659)
        (
            PAUSE_NEWS,
            PAUSE_OPEN,
            PAUSE_OPEN + dt.timedelta(minutes=20),
            15,
            {"C": False, "A": False, "B": False},
        ),
        (
            PAUSE_NEWS,
            PAUSE_OPEN,
            PAUSE_OPEN + dt.timedelta(minutes=20),
            60,
            {"C": True, "A": True, "B": True},
        ),
        # Přesně na konci okna: horní mez je otevřená ve všech variantách
        (NEWS, OPEN, OPEN + dt.timedelta(minutes=60), 60, {"C": False, "A": False, "B": False}),
        # Přesně zpráva + 61 min: uzavřená mez C (a tedy B); o sekundu později jen A
        (NEWS, OPEN, NEWS + dt.timedelta(minutes=61), 60, {"C": True, "A": True, "B": True}),
        (
            NEWS,
            OPEN,
            NEWS + dt.timedelta(minutes=61, seconds=1),
            60,
            {"C": False, "A": True, "B": False},
        ),
    ],
)
def test_varianty_k1_hranice(
    news: dt.datetime,
    start: dt.datetime,
    other: dt.datetime,
    window: int,
    expected: dict[str, bool],
) -> None:
    script = load_script()
    contaminators = Contaminators.build([(news, "MACRO", 3), (other, "FED", 2)])
    assert script.variant_flags(contaminators, "MACRO", news, start, window) == expected


def test_c_je_stav_do_1679_a_a_predikat_jobu() -> None:
    """C = rozsah do #1679 (eventy jen 61 min po zprávě), A = dnešní predikát jobu
    a hodinového K1: `window_contaminated` nad `Contaminators.around` od začátku oken."""
    script = load_script()
    others = [NEWS + dt.timedelta(minutes=m) for m in (0, 1, 30, 61, 62)]
    others += [OPEN + dt.timedelta(minutes=m) for m in (-1, 0, 4, 59, 60)]
    contaminators = Contaminators.build([(ts, "FED", 2) for ts in others])
    for start in (NEWS, NEWS + dt.timedelta(minutes=20), OPEN):
        for window in (1, 5, 15, 60):
            old = window_contaminated(
                NEWS, start, window, contaminators.around(NEWS, "MACRO", NEWS)
            )
            new = window_contaminated(
                NEWS, start, window, contaminators.around(NEWS, "MACRO", start)
            )
            flags = script.variant_flags(contaminators, "MACRO", NEWS, start, window)
            assert flags["C"] is old
            assert flags["A"] is new
