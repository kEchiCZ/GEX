"""Parita replay × živý feature log (#794 fáze 1) a brána stín ↔ 1A (#1590, ADR-0057 bod 7).

Skript žije ve `scripts/`, načítá se přes importlib (vzor test_backtest_replay).
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("replay_parity", SCRIPTS / "replay_parity.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


parity = _load()
NAN = float("nan")


def _minutes(count: int) -> pd.DatetimeIndex:
    return pd.date_range("2026-11-03 14:00", periods=count, freq="min", tz="UTC")


def test_nan_v_obou_radach_neni_shoda() -> None:
    index = _minutes(3)
    live = pd.Series([1.0, NAN, NAN], index=index)
    replay = pd.Series([1.0, NAN, 2.0], index=index)
    result = parity.cum_parity(live, replay)
    assert result.gap_both == 1
    assert result.gap_one_side == 1


def test_compare_nepocita_nan_nan_do_shody(capsys: pytest.CaptureFixture[str]) -> None:
    index = _minutes(4)
    columns = [*parity.DIRECT_FIELDS, "atr", "band_sharpness", "band_sharpness_pct", "band_depth"]
    live = pd.DataFrame({name: [1.0, 1.0, NAN, NAN] for name in columns})
    replay = pd.DataFrame({name: [1.0, 1.0, NAN, 5.0] for name in columns})
    for frame in (live, replay):
        frame["ts"] = index
        frame["gex_regime"] = "positive"
    parity.compare(live, replay)
    line = next(row for row in capsys.readouterr().out.splitlines() if row.startswith("close "))
    # 2 ze 4 minut sedí; NaN × NaN (14:02) ani NaN × 5 (14:03) shoda nejsou
    assert "50.0%" in line
    assert "NaN v obou 1" in line


def test_prirustek_pres_mezeru_se_nepocita() -> None:
    index = _minutes(25)
    live = pd.Series([float(i) for i in range(25)], index=index)
    replay = live * 0.3
    live.iloc[12] = NAN  # mezera 1A ve 14:12
    result = parity.cum_parity(live, replay, minutes=10)
    # okna končící 14:10–14:24: přes 14:12 vede každé okno s koncem 14:12–14:22
    assert result.windows == 15 - 11
    assert result.r is None or math.isnan(result.r) or result.r == pytest.approx(1.0)
    assert result.sign_share == 1.0


def test_obraceny_smer_ma_nulovou_shodu_znamenka() -> None:
    index = _minutes(30)
    values = [((-1) ** (i // 10)) * float(i % 10) for i in range(30)]
    live = pd.Series(values, index=index).cumsum()
    replay = -live
    result = parity.cum_parity(live, replay, minutes=10)
    assert result.windows > 0
    assert result.sign_share == 0.0
    assert result.r == pytest.approx(-1.0)


def _session(r: float | None, sign: float | None, gap: int = 0) -> Any:
    return parity.CumParity(
        minutes=1000, gap_one_side=gap, gap_both=0, windows=990, r=r, sign_share=sign
    )


def test_brana_b_projde_u_stropu_posunu_o_minutu() -> None:
    # strop 14.–20. 10. (ES): r 0,843–0,950, znaménko 86–89 %, mezera ≤ 1,1 %
    results = [_session(r, 0.87, gap=11) for r in (0.843, 0.916, 0.95, 0.92, 0.9)]
    assert parity.gate_failures(results) == []


def test_brana_b_neprojde_pod_prahy() -> None:
    results = [_session(0.9, 0.9) for _ in range(4)] + [_session(0.74, 0.79, gap=250)]
    failures = parity.gate_failures(results)
    assert any("nejnižší r 0.740" in f for f in failures)
    assert any("shoda znaménka 79.0%" in f for f in failures)
    assert any("mezera jen v jedné řadě" in f for f in failures)
    # medián 0,9 je nad 0,85 — ten důvod v seznamu není
    assert not any(f.startswith("medián") for f in failures)


def test_brana_b_chce_pet_seanci_s_r() -> None:
    failures = parity.gate_failures([_session(0.9, 0.9) for _ in range(3)] + [_session(None, 0.9)])
    assert "seancí 4 < 5" in failures
    assert "seancí bez r 1" in failures
