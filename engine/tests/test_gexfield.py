"""Testy Dyn GEX profilu a pole (ADR-0009, #203): BS gamma, znaménka, crunch, persistence."""

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from gexlens_engine.compute.gexfield import (
    ProfileContract,
    bs_gamma,
    gamma_field,
    gamma_profile,
)
from gexlens_engine.config import Settings
from gexlens_engine.storage.parquet_store import GexFieldRow, GexProfileRow, SnapshotWriter

TS = dt.datetime(2026, 7, 22, 14, 0, tzinfo=dt.UTC)
SETTLE = dt.datetime(2026, 7, 22, 20, 0, tzinfo=dt.UTC)


def profile_values(contracts: list[ProfileContract], ts: dt.datetime = TS) -> list[float]:
    profile = gamma_profile(
        contracts,
        ts_min=ts,
        settle=SETTLE,
        grid_start=7400.0,
        grid_stop=7600.0,
        grid_step=5.0,
        multiplier=50.0,
    )
    return list(profile.values)


def test_call_profile_peaks_at_strike_and_is_positive() -> None:
    values = profile_values([ProfileContract(7500.0, "C", 0.15, 1000.0)])
    grid = [7400.0 + 5.0 * i for i in range(len(values))]
    peak_price = grid[values.index(max(values))]
    assert abs(peak_price - 7500.0) <= 5.0  # ATM vrchol
    assert all(v >= 0.0 for v in values)  # call = kladná dealer gamma


def test_put_contributes_negative_and_signs_offset() -> None:
    put_only = profile_values([ProfileContract(7500.0, "P", 0.15, 1000.0)])
    assert min(put_only) < 0.0 and max(put_only) <= 0.0
    # Stejný strike, IV i OI → call a put se přesně vyruší (NaiveDealerModel)
    both = profile_values(
        [
            ProfileContract(7500.0, "C", 0.15, 1000.0),
            ProfileContract(7500.0, "P", 0.15, 1000.0),
        ]
    )
    assert max(abs(v) for v in both) == pytest.approx(0.0, abs=1e-9)


def test_expiry_crunch_gamma_grows_with_shrinking_tau() -> None:
    early = profile_values([ProfileContract(7500.0, "C", 0.15, 1000.0)], ts=TS)
    late = profile_values(
        [ProfileContract(7500.0, "C", 0.15, 1000.0)],
        ts=dt.datetime(2026, 7, 22, 19, 30, tzinfo=dt.UTC),
    )
    assert max(late) > max(early)  # ATM gamma do expirace roste (crunch)


def test_tau_floor_prevents_divergence_at_settle() -> None:
    at_settle = profile_values([ProfileContract(7500.0, "C", 0.15, 1000.0)], ts=SETTLE)
    assert all(v == v and v != float("inf") for v in at_settle)  # žádné NaN/inf
    assert max(at_settle) > 0.0


def test_contracts_without_iv_or_oi_are_skipped() -> None:
    values = profile_values(
        [
            ProfileContract(7500.0, "C", 0.0, 1000.0),  # bez IV
            ProfileContract(7500.0, "C", 0.15, 0.0),  # bez OI
        ]
    )
    assert all(v == 0.0 for v in values)
    assert bs_gamma(7500.0, 7500.0, 0.0, 0.1) == 0.0


def field_kwargs() -> dict[str, object]:
    return {
        "ts_min": TS,
        "settle": SETTLE,
        "grid_start": 7400.0,
        "grid_stop": 7600.0,
        "grid_step": 5.0,
        "multiplier": 50.0,
    }


def test_gamma_field_columns_cover_time_to_settle() -> None:
    """Fáze 2: sloupce po col_step_min od ts_min+krok až k settle."""
    field = gamma_field(
        [ProfileContract(7500.0, "C", 0.15, 1000.0)],
        col_step_min=10,
        **field_kwargs(),  # type: ignore[arg-type]
    )
    assert field is not None
    assert field.col_start == TS + dt.timedelta(minutes=10)
    assert len(field.values) == 36  # 6 h do settle / 10 min
    assert all(len(column) == 41 for column in field.values)  # mřížka 7400..7600 po 5


def test_gamma_field_first_column_matches_profile() -> None:
    """První sloupec pole = profil spočtený k času prvního sloupce (stejná τ)."""
    contracts = [
        ProfileContract(7500.0, "C", 0.15, 1000.0),
        ProfileContract(7450.0, "P", 0.18, 400.0),
    ]
    field = gamma_field(contracts, col_step_min=10, **field_kwargs())  # type: ignore[arg-type]
    assert field is not None
    profile = gamma_profile(
        contracts,
        ts_min=TS + dt.timedelta(minutes=10),
        settle=SETTLE,
        grid_start=7400.0,
        grid_stop=7600.0,
        grid_step=5.0,
        multiplier=50.0,
    )
    for got, expected in zip(field.values[0], profile.values, strict=True):
        assert got == pytest.approx(expected, rel=1e-9)


def test_gamma_field_atm_crunch_grows_toward_settle() -> None:
    """Pozdější sloupce (menší τ) mají vyšší ATM špičku — vizuální crunch."""
    field = gamma_field(
        [ProfileContract(7500.0, "C", 0.15, 1000.0)],
        **field_kwargs(),  # type: ignore[arg-type]
    )
    assert field is not None
    assert max(field.values[-1]) > max(field.values[0])


def test_gamma_field_horizon_cap_and_empty_inputs() -> None:
    """Vzdálená expirace se stropuje na horizon_min; bez kontraktů/času pole není."""
    far_settle = TS + dt.timedelta(hours=48)
    capped = gamma_field(
        [ProfileContract(7500.0, "C", 0.15, 1000.0)],
        ts_min=TS,
        settle=far_settle,
        grid_start=7400.0,
        grid_stop=7600.0,
        grid_step=5.0,
        multiplier=50.0,
        col_step_min=10,
        horizon_min=24 * 60,
    )
    assert capped is not None
    assert len(capped.values) == 144  # 24 h / 10 min
    assert gamma_field([], **field_kwargs()) is None  # type: ignore[arg-type]
    past = gamma_field(
        [ProfileContract(7500.0, "C", 0.15, 1000.0)],
        ts_min=SETTLE,
        settle=SETTLE,
        grid_start=7400.0,
        grid_stop=7600.0,
        grid_step=5.0,
        multiplier=50.0,
    )
    assert past is None  # po settle už není co modelovat


def test_gexfield_partition_keeps_only_last_state(tmp_path: Path) -> None:
    """Fáze 2: write_gexfield přepisuje — partice drží jen poslední stav minuty."""
    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    day = dt.date(2026, 7, 22)

    def row(minute: int, values: list[float]) -> GexFieldRow:
        return GexFieldRow(
            ts_min=TS + dt.timedelta(minutes=minute),
            grid_start=7400.0,
            grid_step=2.5,
            col_start=TS + dt.timedelta(minutes=minute + 10),
            col_step_min=10,
            col_count=2,
            values=values,
        )

    writer.write_gexfield("ES", "20260722", day, row(0, [1.0, 2.0, 3.0, 4.0]))
    path = writer.write_gexfield("ES", "20260722", day, row(1, [5.0, 6.0, 7.0, 8.0]))

    assert path == tmp_path / "derived" / "ES" / "20260722" / "gexfield" / "2026-07-22.parquet"
    frame = pd.read_parquet(path)
    assert len(frame) == 1  # replace, ne append
    assert frame["ts_min"][0].to_pydatetime() == TS + dt.timedelta(minutes=1)
    assert list(frame["values"][0]) == [5.0, 6.0, 7.0, 8.0]
    assert int(frame["col_count"][0]) == 2


def test_gexprofile_persisted_with_list_column(tmp_path: Path) -> None:
    """ADR-0009: profil jde do derived/{sym}/{exp}/gexprofile s list sloupcem values."""
    writer = SnapshotWriter(Settings(data_dir=tmp_path))
    day = dt.date(2026, 7, 22)
    row = GexProfileRow(ts_min=TS, grid_start=7400.0, grid_step=2.5, values=[1.0, -2.5, 3.0])

    path = writer.write_gexprofile("ES", "20260722", day, [row])

    assert path == tmp_path / "derived" / "ES" / "20260722" / "gexprofile" / "2026-07-22.parquet"
    frame = pd.read_parquet(path)
    assert list(frame.columns) == ["ts_min", "grid_start", "grid_step", "values"]
    assert list(frame["values"][0]) == [1.0, -2.5, 3.0]
