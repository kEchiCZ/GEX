"""Testy denní FA validace (#232): metriky, úložiště a sběrný job."""

import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.favalidation import (
    FaValidationPoint,
    compute_fa_validation,
)
from gexlens_engine.storage.fa_validation import (
    FaValidationRecord,
    FaValidationRepository,
    collect_fa_validation,
    volumes_at_cutoff,
)
from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord

DAY = dt.date(2026, 7, 23)
NEXT = dt.date(2026, 7, 24)
EXPIRY = "20260724"


def _sample_maps(
    n: int = 12,
) -> tuple[dict[tuple[float, str], float], dict[tuple[float, str], float]]:
    """n stran s volume 10·i a ΔOI = 0.4·volume (open-ratio přesně 0.4)."""
    volumes = {(7000.0 + 5 * i, "C"): 10.0 * (i + 1) for i in range(n)}
    oi_after = {key: 0.4 * volume for key, volume in volumes.items()}
    return volumes, oi_after


def test_compute_metriky_perfektni_korelace() -> None:
    volumes, oi_after = _sample_maps()
    point = compute_fa_validation(volumes, {}, oi_after)
    assert point is not None
    assert point.contracts == 12
    assert point.open_ratio == pytest.approx(0.4)
    assert point.spearman == pytest.approx(1.0)
    assert point.silent_share == 0.0
    assert point.doi_net_sum == pytest.approx(point.doi_abs_sum)


def test_compute_silent_share_a_net() -> None:
    volumes, oi_after = _sample_maps()
    # Tichá strana: ΔOI bez zachyceného volume (např. block trade mimo klasifikaci)
    oi_after[(6900.0, "P")] = 100.0
    # Zavírání: záporná ΔOI snižuje net, ale zvyšuje abs
    oi_before = {(7000.0, "C"): 50.0}
    point = compute_fa_validation(volumes, oi_before, oi_after)
    assert point is not None
    assert point.silent_share == pytest.approx(100.0 / point.doi_abs_sum)
    assert point.doi_net_sum < point.doi_abs_sum


def test_compute_nedostatecny_vzorek_vraci_none() -> None:
    volumes, oi_after = _sample_maps(n=5)  # < MIN_TRADED_CONTRACTS
    assert compute_fa_validation(volumes, {}, oi_after) is None
    assert compute_fa_validation({}, {}, {}) is None
    # Dost stran, ale volume pod prahem
    tiny = {(7000.0 + i, "C"): 1.0 for i in range(12)}
    assert compute_fa_validation(tiny, {}, {}) is None


def test_compute_nulova_variance_spearman_nula() -> None:
    volumes = {(7000.0 + i, "C"): 50.0 for i in range(12)}  # samé remízy
    oi_after = {key: 10.0 * i for i, key in enumerate(volumes)}
    point = compute_fa_validation(volumes, {}, oi_after)
    assert point is not None
    assert point.spearman == 0.0


def _write_snapshot(path: Path, day: dt.date, rows: list[tuple[int, float, str, float]]) -> None:
    """Zápis minimální snapshot partice: (hodina UTC, strike, right, volume)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "ts_min": pa.array(
                [dt.datetime.combine(day, dt.time(h), tzinfo=dt.UTC) for h, _, _, _ in rows],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "strike": pa.array([s for _, s, _, _ in rows], type=pa.float64()),
            "right": pa.array([r for _, _, r, _ in rows], type=pa.string()),
            "volume": pa.array([v for _, _, _, v in rows], type=pa.float64()),
        }
    )
    pq.write_table(table, path)


def test_volumes_at_cutoff_rez_21_utc(tmp_path: Path) -> None:
    path = tmp_path / "snap.parquet"
    _write_snapshot(
        path,
        DAY,
        [
            (14, 7000.0, "C", 50.0),
            (20, 7000.0, "C", 120.0),  # poslední hodnota před řezem vyhrává
            (22, 7000.0, "C", 5.0),  # po resetu counteru — ignorovat
            (15, 7005.0, "P", 30.0),
        ],
    )
    volumes = volumes_at_cutoff(path, DAY)
    assert volumes == {(7000.0, "C"): 120.0, (7005.0, "P"): 30.0}


def test_repository_upsert_a_exists() -> None:
    repo = FaValidationRepository(create_engine("sqlite://"))
    repo.ensure_schema()
    point = FaValidationPoint(
        contracts=12,
        volume_sum=780.0,
        doi_abs_sum=312.0,
        doi_net_sum=312.0,
        open_ratio=0.4,
        spearman=1.0,
        silent_share=0.0,
    )
    record = FaValidationRecord("ES", EXPIRY, DAY, NEXT, point)
    assert not repo.exists("ES", EXPIRY, DAY)
    repo.upsert(record)
    repo.upsert(record)  # idempotence vůči restartu
    assert repo.exists("ES", EXPIRY, DAY)
    assert not repo.exists("ES", EXPIRY, NEXT)


def test_collect_spocita_ulozi_a_dedupuje(tmp_path: Path) -> None:
    db = create_engine("sqlite://")
    oi_repo = OIEodRepository(db)
    oi_repo.ensure_schema()
    fa_repo = FaValidationRepository(db)
    fa_repo.ensure_schema()

    strikes = [7000.0 + 5 * i for i in range(12)]
    _write_snapshot(
        tmp_path / "ES" / EXPIRY / f"{DAY.isoformat()}.parquet",
        DAY,
        [(20, s, "C", 10.0 * (i + 1)) for i, s in enumerate(strikes)],
    )
    oi_repo.upsert_many([OIRecord("ES", EXPIRY, s, "C", DAY, 100.0) for s in strikes])
    oi_repo.upsert_many(
        [OIRecord("ES", EXPIRY, s, "C", NEXT, 100.0 + 4.0 * (i + 1)) for i, s in enumerate(strikes)]
    )
    # Mrtvá expirace: snapshot i včerejší OI, ale dnešní OI chybí → přeskočit
    dead = "20260723"
    _write_snapshot(
        tmp_path / "ES" / dead / f"{DAY.isoformat()}.parquet",
        DAY,
        [(20, s, "P", 20.0) for s in strikes],
    )
    oi_repo.upsert_many([OIRecord("ES", dead, s, "P", DAY, 50.0) for s in strikes])

    records = collect_fa_validation("ES", tmp_path, oi_repo, fa_repo, NEXT)
    assert [(r.expiry, r.day, r.next_day) for r in records] == [(EXPIRY, DAY, NEXT)]
    point = records[0].point
    assert point.open_ratio == pytest.approx(0.4)
    assert point.spearman == pytest.approx(1.0)
    assert fa_repo.exists("ES", EXPIRY, DAY)
    # Druhý běh (restart pipeline) už bod nepřepočítává
    assert collect_fa_validation("ES", tmp_path, oi_repo, fa_repo, NEXT) == []
