"""Walk-forward kritéria brány šablon (#1334) — `scripts/measure_template_gate_1334.py`.

Skript žije ve `scripts/`, načítá se přes importlib (vzor test_measure_fa_ratio_1612).
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

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "measure_template_gate_1334.py"
EVENT_TS = dt.datetime(2026, 10, 1, 15, 0, tzinfo=dt.UTC)  # čtvrtek, seance 1. 10.


@pytest.fixture(scope="module")
def script() -> Any:
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("measure_template_gate_1334", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass skriptu hledá svůj modul v sys.modules
    spec.loader.exec_module(module)
    return module


def _row(
    closed: dt.datetime, outcome_r: float, *, created: dt.datetime | None = None
) -> RealizedSetup:
    """ES setup se stopem 2 b (v rozpočtu): čisté R = R − 2,49 / (2 × 5)."""
    return RealizedSetup(
        symbol="ES",
        template="wall_bounce",
        status="closed_target" if outcome_r > 0 else "closed_stop",
        outcome_r=outcome_r,
        closed_ts=closed,
        tradeable=False,
        affordable=True,
        entry=7000.0,
        stop=6998.0,
        created_ts=created or closed - dt.timedelta(minutes=30),
    )


def _events(script: Any, rows: list[RealizedSetup]) -> list[Any]:
    events: list[Any] = script.build_events(
        rows,
        SetupParams(template_gate_min_samples=30),
        date_from=dt.date(2026, 10, 1),
        date_to=dt.date(2026, 10, 1),
        skipped=defaultdict(int),
    )
    return events


def test_window_is_point_in_time(script: Any) -> None:
    """Okno bere jen řádky uzavřené před vznikem setupu, ne jeho vlastní ani pozdější."""
    history = [_row(EVENT_TS - dt.timedelta(days=1, minutes=i), 1.0) for i in range(30)]
    event = _row(EVENT_TS + dt.timedelta(minutes=20), -1.0, created=EVENT_TS)
    later_loss = _row(EVENT_TS + dt.timedelta(minutes=10), -1.0, created=EVENT_TS)
    events = _events(script, [*history, event, later_loss])

    assert len(events) == 2
    first = events[0]
    assert first.window_n == 30  # setupy uzavřené po vzniku (vč. vlastního) se nepočítají
    assert first.net == pytest.approx(-1.0 - 2.49 / 10)
    assert first.verdicts["LB"] is True  # 30 × +1 R: σ = 0, LB = Ø > 0
    assert first.verdicts["ØRč"] is True


def test_below_min_samples_never_passes(script: Any) -> None:
    """Pod `template_gate_min_samples` nepustí žádné kritérium, ani s kladným Ø."""
    history = [_row(EVENT_TS - dt.timedelta(days=1, minutes=i), 1.0) for i in range(29)]
    event = _row(EVENT_TS + dt.timedelta(minutes=20), 1.0, created=EVENT_TS)
    (only,) = _events(script, [*history, event])

    assert only.window_n == 29
    assert not any(only.verdicts.values())


def test_window_drops_rows_older_than_gate_days(script: Any) -> None:
    """Řádek uzavřený před začátkem okna (`gate_window_start`, 60 seancí ≈ 84 dní) nepočítá."""
    old = [_row(EVENT_TS - dt.timedelta(days=90, minutes=i), 1.0) for i in range(5)]
    recent = [_row(EVENT_TS - dt.timedelta(days=1, minutes=i), 1.0) for i in range(30)]
    event = _row(EVENT_TS + dt.timedelta(minutes=20), 1.0, created=EVENT_TS)
    (only,) = _events(script, [*old, *recent, event])

    assert only.window_n == 30


def test_net_criteria_differ_from_gross(script: Any) -> None:
    """Hrubě kladné, čistě záporné okno: ØR pustí, ØRč ne (náklad ~0,25 R na stop 2 b)."""
    history = [_row(EVENT_TS - dt.timedelta(days=1, minutes=i), 0.1) for i in range(30)]
    event = _row(EVENT_TS + dt.timedelta(minutes=20), 1.0, created=EVENT_TS)
    (only,) = _events(script, [*history, event])

    assert only.verdicts["ØR"] is True
    assert only.verdicts["ØRč"] is False
    assert only.verdicts["LBč"] is False


def test_spearman_handles_ties_and_small_samples(script: Any) -> None:
    assert script.spearman([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) is None
    rho, p = script.spearman([1.0, 2.0, 2.0, 3.0, 4.0], [10.0, 20.0, 20.0, 30.0, 40.0])
    assert rho == pytest.approx(1.0)
    assert p == 0.0
    rho, _ = script.spearman([1.0, 2.0, 3.0, 4.0], [4.0, 3.0, 2.0, 1.0])
    assert rho == pytest.approx(-1.0)
