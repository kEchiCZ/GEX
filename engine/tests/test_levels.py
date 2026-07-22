"""Testy GEX Levels (issue #14): golden dataset, edge cases, persistence do derived/."""

import datetime as dt
import json
import math
from pathlib import Path

import pandas as pd
import pytest

from gexlens_engine.compute.levels import compute_levels
from gexlens_engine.config import Settings
from gexlens_engine.storage.parquet_store import Levels2Row, LevelsRow, SnapshotWriter

GOLDEN_PATH = Path(__file__).parent / "golden" / "levels_basic.json"


def load_golden() -> dict[str, object]:
    data: dict[str, object] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return data


def parse_profile(raw: dict[str, float]) -> dict[float, float]:
    return {float(strike): net for strike, net in raw.items()}


def test_golden_levels() -> None:
    golden = load_golden()
    profile = parse_profile(golden["net_by_strike"])  # type: ignore[arg-type]
    expected = golden["expected"]
    assert isinstance(expected, dict)

    levels = compute_levels(profile, spot=float(golden["spot"]))  # type: ignore[arg-type]

    assert levels.flip == pytest.approx(expected["flip"])
    assert levels.call_wall == expected["call_wall"]
    assert levels.put_wall == expected["put_wall"]
    assert levels.centroid == pytest.approx(expected["centroid"])
    assert levels.total_gex == pytest.approx(expected["total_gex"])


def test_golden_edge_all_positive_no_flip() -> None:
    """AC: celý profil kladný → flip neexistuje, korektně reportováno jako None."""
    edge = load_golden()["edge_all_positive"]
    assert isinstance(edge, dict)
    profile = parse_profile(edge["net_by_strike"])

    levels = compute_levels(profile, spot=float(edge["spot"]))

    expected = edge["expected"]
    assert levels.flip is None
    assert levels.call_wall == expected["call_wall"]
    assert levels.put_wall == expected["put_wall"]
    assert levels.centroid == pytest.approx(expected["centroid"])
    assert levels.total_gex == pytest.approx(expected["total_gex"])


def test_all_negative_profile_has_no_flip() -> None:
    levels = compute_levels({7550.0: -100.0, 7600.0: -300.0}, spot=7600.0)
    assert levels.flip is None
    assert levels.total_gex == -400.0


def test_flip_exact_zero_cumulative_hits_strike() -> None:
    # Kumulativně: -100, 0, +200 → nula přesně na 7550
    levels = compute_levels({7500.0: -100.0, 7550.0: 100.0, 7600.0: 200.0}, spot=7500.0)
    assert levels.flip == 7550.0


def test_multiple_crossings_pick_nearest_to_spot() -> None:
    # Kumulativně: +100, -100, +100 → průchody mezi 7500–7550 a 7550–7600
    profile = {7500.0: 100.0, 7550.0: -200.0, 7600.0: 200.0}
    near_low = compute_levels(profile, spot=7500.0)
    near_high = compute_levels(profile, spot=7600.0)
    assert near_low.flip == pytest.approx(7525.0)
    assert near_high.flip == pytest.approx(7575.0)


def test_flip_ignores_leading_zero_strikes() -> None:
    """#197: prázdné okrajové strikes nejsou nulový průchod — flip neskáče na okraj pásma."""
    # Vodicí nuly + celý kladný profil → žádný skutečný průchod → None
    # (starý kód vracel 7340 = okraj pásma, v grafu žluté svislé sloupy)
    all_positive = compute_levels(
        {7340.0: 0.0, 7350.0: 0.0, 7400.0: 100.0, 7500.0: 200.0}, spot=7450.0
    )
    assert all_positive.flip is None

    # Vodicí nuly + skutečný průchod → interpolace jako dřív, okraj se neplete
    real_crossing = compute_levels({7340.0: 0.0, 7400.0: -100.0, 7500.0: 150.0}, spot=7450.0)
    assert real_crossing.flip == pytest.approx(7400.0 + (100.0 / 150.0) * 100.0)

    # Celý nulový profil flip nemá (dřív vracel první strike)
    zero = compute_levels({7500.0: 0.0, 7550.0: 0.0}, spot=7500.0)
    assert zero.flip is None


def test_walls_none_when_spot_outside_profile() -> None:
    profile = {7500.0: 100.0, 7550.0: -50.0}
    below_all = compute_levels(profile, spot=7400.0)
    above_all = compute_levels(profile, spot=7700.0)
    assert below_all.put_wall is None  # pod spotem nic není
    assert above_all.call_wall is None


def test_zero_profile_and_empty_profile() -> None:
    zero = compute_levels({7500.0: 0.0, 7550.0: 0.0}, spot=7500.0)
    assert zero.centroid is None  # Σ|NetGEX| = 0
    empty = compute_levels({}, spot=7500.0)
    assert empty.flip is None
    assert empty.call_wall is None
    assert empty.put_wall is None
    assert empty.centroid is None
    assert empty.total_gex == 0.0


def test_secondary_wall_reported_when_comparable(  # ADR-0008, #92
) -> None:
    """Dvě rovnocenné koncentrace → sekundární zeď; přeskakování mezi 7450/7500."""
    profile = {
        7400.0: -80.0,
        7450.0: -950.0,  # sekundární put koncentrace (95 % primární)
        7475.0: -100.0,
        7500.0: -1000.0,  # primární put wall
        7550.0: 200.0,
        7600.0: 900.0,  # sekundární call koncentrace (90 % primární)
        7625.0: 150.0,
        7650.0: 1000.0,  # primární call wall
    }
    levels = compute_levels(profile, spot=7520.0)
    assert levels.put_wall == 7500.0
    assert levels.put_wall_2 == 7450.0
    assert levels.call_wall == 7650.0
    assert levels.call_wall_2 == 7600.0


def test_secondary_wall_none_below_ratio_or_for_shoulder() -> None:
    """Slabá koncentrace (< SECONDARY_WALL_RATIO) ani rameno primární nejsou 2. zeď."""
    weak = compute_levels(
        {7450.0: -300.0, 7500.0: -1000.0, 7600.0: 500.0},  # 30 % < ratio
        spot=7550.0,
    )
    assert weak.put_wall == 7500.0
    assert weak.put_wall_2 is None

    # Soused primární zdi (rameno té samé koncentrace) není lokální vrchol
    shoulder = compute_levels(
        {7495.0: -900.0, 7500.0: -1000.0, 7505.0: -100.0, 7600.0: 500.0},
        spot=7550.0,
    )
    assert shoulder.put_wall == 7500.0
    assert shoulder.put_wall_2 is None

    # Bez primární zdi není ani sekundární
    empty = compute_levels({7600.0: 500.0}, spot=7550.0)
    assert empty.put_wall is None
    assert empty.put_wall_2 is None


def test_levels2_series_persisted_to_derived(tmp_path: Path) -> None:
    """ADR-0008: sekundární zdi jdou do vlastní řady derived/{sym}/{exp}/levels2."""
    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    day = dt.date(2026, 7, 16)
    rows = [
        Levels2Row(
            ts_min=dt.datetime(2026, 7, 16, 15, minute, tzinfo=dt.UTC),
            call_wall_2=7600.0 if minute == 0 else None,
            put_wall_2=7450.0,
        )
        for minute in range(2)
    ]

    path = writer.write_levels2("ES", "20260716", day, rows)

    assert path == tmp_path / "derived" / "ES" / "20260716" / "levels2" / "2026-07-16.parquet"
    frame = pd.read_parquet(path)
    assert list(frame.columns) == ["ts_min", "call_wall_2", "put_wall_2"]
    assert len(frame) == 2
    assert frame["call_wall_2"][0] == pytest.approx(7600.0)
    assert math.isnan(frame["call_wall_2"][1])


def test_levels_series_persisted_to_derived(tmp_path: Path) -> None:
    """SPEC 4.2: levels se ukládají jako časová řada do derived/ partice."""
    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    day = dt.date(2026, 7, 16)
    rows = [
        LevelsRow(
            ts_min=dt.datetime(2026, 7, 16, 15, minute, tzinfo=dt.UTC),
            flip=7660.0 if minute == 0 else None,  # druhá minuta: flip neexistuje
            call_wall=7650.0,
            put_wall=7500.0,
            centroid=7598.21,
            total_gex=400.0,
        )
        for minute in range(2)
    ]

    path = writer.write_levels("ES", "20260716", day, rows)

    assert path == tmp_path / "derived" / "ES" / "20260716" / "levels" / "2026-07-16.parquet"
    frame = pd.read_parquet(path)
    expected_columns = ["ts_min", "flip", "call_wall", "put_wall", "centroid", "total_gex"]
    assert list(frame.columns) == expected_columns
    assert len(frame) == 2
    assert frame["flip"][0] == pytest.approx(7660.0)
    assert math.isnan(frame["flip"][1])  # None → NaN, řada zůstává čitelná
