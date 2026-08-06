"""Testy ranní kalibrace α (#232 fáze 2): čistý výpočet, úložiště, sběr bodu."""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.facalibration import (
    AlphaCalibrationPoint,
    calibrate_alpha,
    update_alpha,
)
from gexlens_engine.config import Settings
from gexlens_engine.storage.fa_calibration import (
    FaAlphaRepository,
    collect_alpha_calibration,
    netflow_at_cutoff,
)
from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord
from gexlens_engine.storage.parquet_store import NetFlowRow, SnapshotWriter

PREV = dt.date(2026, 8, 5)
TODAY = dt.date(2026, 8, 6)
EXPIRY = "20260805"


def test_calibrate_alpha_median_a_strany() -> None:
    """Medián poměrů ΔOI/net přes kvalifikované strany; buy/sell zvlášť pro audit."""
    netflow = {
        (7500.0, "C"): 100.0,
        (7510.0, "C"): 100.0,
        (7520.0, "C"): 100.0,
        (7530.0, "C"): 200.0,
        (7500.0, "P"): -50.0,
        (7510.0, "P"): -100.0,
        (7600.0, "C"): 10.0,  # pod prahem |net| — nekvalifikuje se
    }
    doi = {
        (7500.0, "C"): 40.0,  # 0.4
        (7510.0, "C"): 40.0,  # 0.4
        (7520.0, "C"): 30.0,  # 0.3
        (7530.0, "C"): 100.0,  # 0.5
        (7500.0, "P"): -25.0,  # 0.5 (net prodej, OI klesl — směr sedí)
        (7510.0, "P"): -50.0,  # 0.5
        (7600.0, "C"): 999.0,  # ignorováno
    }
    point = calibrate_alpha(netflow, doi)
    assert point is not None
    assert point.samples == 6
    assert point.ratio_median == pytest.approx(0.45)  # [0.3,0.4,0.4,0.5,0.5,0.5]
    assert point.ratio_buy == pytest.approx(0.4)
    assert point.ratio_sell == pytest.approx(0.5)


def test_calibrate_alpha_znamenko_proti_smeru() -> None:
    """Net nákup s poklesem OI dává záporný poměr — medián ho po právu stáhne."""
    netflow = {(7500.0 + i, "C"): 100.0 for i in range(5)}
    doi = {(7500.0 + i, "C"): -40.0 for i in range(5)}
    point = calibrate_alpha(netflow, doi)
    assert point is not None
    assert point.ratio_median == pytest.approx(-0.4)


def test_calibrate_alpha_maly_vzorek_vraci_none() -> None:
    netflow = {(7500.0, "C"): 100.0, (7510.0, "C"): 100.0}
    assert calibrate_alpha(netflow, {(7500.0, "C"): 40.0}) is None
    assert calibrate_alpha({}, {}) is None


def test_update_alpha_prvni_bod_a_ema() -> None:
    assert update_alpha(None, 0.45) == 0.45
    # Clamp: záporný medián (rozbitá data) nesmí α stáhnout pod 0, nad 1 nejde
    assert update_alpha(None, -0.4) == 0.0
    assert update_alpha(None, 1.7) == 1.0
    # EMA λ 0.3: 0.4 + 0.3·(0.6 − 0.4)
    assert update_alpha(0.4, 0.6) == pytest.approx(0.46)


def test_repository_roundtrip(tmp_path: Path) -> None:
    repo = FaAlphaRepository(create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}"))
    repo.ensure_schema()
    assert repo.get("ES") is None
    assert repo.list_all() == []
    assert not repo.history_exists("ES", PREV)

    point = AlphaCalibrationPoint(samples=6, ratio_median=0.45, ratio_buy=0.4, ratio_sell=0.5)
    now = dt.datetime(2026, 8, 6, 12, 0, tzinfo=dt.UTC)
    repo.record("ES", PREV, EXPIRY, point, alpha_after=0.45, days=1, now=now)

    state = repo.get("ES")
    assert state is not None
    assert state.alpha == 0.45 and state.days == 1
    assert repo.history_exists("ES", PREV)
    # Upsert je idempotentní — opakovaný zápis stejného dne nic nerozbije
    repo.record("ES", PREV, EXPIRY, point, alpha_after=0.45, days=1, now=now)
    assert len(repo.list_all()) == 1


def test_netflow_at_cutoff_rez_21_utc(tmp_path: Path) -> None:
    """Kumulativ po 21:00 UTC patří další seanci — do kalibrace nesmí."""
    settings = Settings(data_dir=tmp_path)
    writer = SnapshotWriter(settings)
    before = dt.datetime.combine(PREV, dt.time(20, 59), tzinfo=dt.UTC)
    after = dt.datetime.combine(PREV, dt.time(21, 30), tzinfo=dt.UTC)
    path = writer.write_netflow(
        "ES",
        EXPIRY,
        PREV,
        [
            NetFlowRow(ts_min=before, strike=7500.0, right="C", net_volume=80.0),
            NetFlowRow(ts_min=after, strike=7500.0, right="C", net_volume=999.0),
        ],
    )
    assert path is not None
    assert netflow_at_cutoff(path, PREV) == {(7500.0, "C"): 80.0}


def _seed_day(
    settings: Settings, oi_repo: OIEodRepository, *, net: float = 100.0, doi: float = 40.0
) -> None:
    """Včerejší netflow (5 stran) + archivy obou dnů s ΔOI = doi na stranu."""
    writer = SnapshotWriter(settings)
    ts = dt.datetime.combine(PREV, dt.time(20, 0), tzinfo=dt.UTC)
    strikes = [7500.0 + 10 * i for i in range(5)]
    writer.write_netflow(
        "ES",
        EXPIRY,
        PREV,
        [NetFlowRow(ts_min=ts, strike=strike, right="C", net_volume=net) for strike in strikes],
    )
    oi_repo.upsert_many([OIRecord("ES", EXPIRY, strike, "C", PREV, 1000.0) for strike in strikes])
    oi_repo.upsert_many(
        [OIRecord("ES", EXPIRY, strike, "C", TODAY, 1000.0 + doi) for strike in strikes]
    )


def test_collect_alpha_calibration_a_dedup(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}")
    oi_repo = OIEodRepository(db)
    oi_repo.ensure_schema()
    alpha_repo = FaAlphaRepository(db)
    alpha_repo.ensure_schema()
    _seed_day(settings, oi_repo)

    result = collect_alpha_calibration("ES", settings.derived_dir, oi_repo, alpha_repo, TODAY)
    assert result is not None
    assert result.day == PREV and result.expiry == EXPIRY
    assert result.point.ratio_median == pytest.approx(0.4)  # 40/100 na každé straně
    assert result.alpha_after == pytest.approx(0.4)  # první bod jde přímo
    assert result.days == 1

    # Idempotence: den už je v historii → druhý běh bod nepřidá
    assert collect_alpha_calibration("ES", settings.derived_dir, oi_repo, alpha_repo, TODAY) is None
    state = alpha_repo.get("ES")
    assert state is not None and state.days == 1


def test_collect_bez_netflow_vraci_none(tmp_path: Path) -> None:
    """Bez řady netflow (žádný tok, starší den) kalibrace nic nedělá."""
    settings = Settings(data_dir=tmp_path / "data")
    db = create_engine(f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}")
    oi_repo = OIEodRepository(db)
    oi_repo.ensure_schema()
    alpha_repo = FaAlphaRepository(db)
    alpha_repo.ensure_schema()
    assert collect_alpha_calibration("ES", settings.derived_dir, oi_repo, alpha_repo, TODAY) is None
