"""Přepočet FA validace a α nad finálními archivy (#1314) — `scripts/recompute_fa.py`.

Skript žije ve `scripts/`, načítá se přes importlib (vzor test_recompute_setup_outcomes).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import create_engine

from gexlens_engine.compute.facalibration import AlphaCalibrationPoint
from gexlens_engine.compute.favalidation import FaValidationPoint
from gexlens_engine.config import Settings
from gexlens_engine.storage.fa_calibration import FaAlphaRepository
from gexlens_engine.storage.fa_validation import FaValidationRecord, FaValidationRepository
from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord
from gexlens_engine.storage.parquet_store import NetFlowRow, SnapshotWriter

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "recompute_fa.py"
PREV = dt.date(2026, 7, 16)  # čtvrtek
TODAY = dt.date(2026, 7, 17)  # pátek
EXPIRY = "20260724"  # expiruje po dni netflow → kalibrace ji bere
STRIKES = [7500.0 + 10 * i for i in range(12)]


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("recompute_fa", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _seed(tmp_path: Path) -> tuple[str, Settings]:
    settings = Settings(data_dir=tmp_path / "data")
    url = f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}"
    db = create_engine(url)
    oi_repo = OIEodRepository(db)
    oi_repo.ensure_schema()
    FaValidationRepository(db).ensure_schema()
    FaAlphaRepository(db).ensure_schema()
    after_pub = {
        day: settings.oi_publication_utc(day) + dt.timedelta(hours=1) for day in (PREV, TODAY)
    }
    # Finální archivy (DB po obnově #463): ΔOI = +40 na každé straně
    oi_repo.upsert_many(
        [OIRecord("ES", EXPIRY, s, "C", PREV, 1000.0) for s in STRIKES], after_pub[PREV]
    )
    oi_repo.upsert_many(
        [OIRecord("ES", EXPIRY, s, "C", TODAY, 1040.0) for s in STRIKES], after_pub[TODAY]
    )
    ts = dt.datetime.combine(PREV, dt.time(20, 0), tzinfo=dt.UTC)
    SnapshotWriter(settings).write_netflow(
        "ES",
        EXPIRY,
        PREV,
        [NetFlowRow(ts_min=ts, strike=s, right="C", net_volume=100.0) for s in STRIKES],
    )
    path = settings.snapshots_dir / "ES" / EXPIRY / f"{PREV.isoformat()}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "ts_min": pa.array([ts] * len(STRIKES), type=pa.timestamp("us", tz="UTC")),
                "strike": pa.array(STRIKES, type=pa.float64()),
                "right": pa.array(["C"] * len(STRIKES), type=pa.string()),
                "volume": pa.array([100.0] * len(STRIKES), type=pa.float64()),
            }
        ),
        path,
    )
    # Zamčené body z předpublikačního běhu: |ΔOI| 0 a α 0 (stav NQ před #1314)
    FaValidationRepository(db).upsert(
        FaValidationRecord(
            "ES", EXPIRY, PREV, TODAY, FaValidationPoint(12, 1200.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        )
    )
    FaAlphaRepository(db).record(
        "ES", PREV, EXPIRY, AlphaCalibrationPoint(12, 0.0, None, None), 0.0, 0
    )
    return url, settings


def test_dry_run_nic_nezapise_apply_opravi_alfu_i_validaci(tmp_path: Path) -> None:
    module = _load()
    url, settings = _seed(tmp_path)
    args = ["--db", url, "--data", str(settings.data_dir), "--symbols", "ES"]
    assert module.main(args) == 0
    alpha_repo = FaAlphaRepository(create_engine(url))
    state = alpha_repo.get("ES")
    assert state is not None and state.alpha == 0.0  # dry-run nepíše
    report = next((settings.data_dir / "reports").glob("recompute-fa-*.md")).read_text("utf-8")
    assert "0.000 → **0.400**" in report

    assert module.main([*args, "--apply", "--yes"]) == 0
    state = alpha_repo.get("ES")
    assert state is not None and state.alpha == pytest.approx(0.4) and state.days == 1
    results = module.replay_symbol(
        "ES", settings, OIEodRepository(create_engine(url)), create_engine(url)
    )
    old_doi, new_doi, _, new_ratio = results.validation[(EXPIRY, PREV)]
    assert old_doi == pytest.approx(480.0)  # přepsáno z 0 na finální Σ|ΔOI|
    assert new_doi == pytest.approx(480.0) and new_ratio == pytest.approx(0.4)


def test_predpublikacni_snimek_je_neoveritelny(tmp_path: Path) -> None:
    module = _load()
    url, settings = _seed(tmp_path)
    oi_repo = OIEodRepository(create_engine(url))
    # Dnešní snímek z 00:05 UTC — obnova po publikaci neproběhla
    oi_repo.upsert_many(
        [OIRecord("ES", EXPIRY, s, "C", TODAY, 1040.0) for s in STRIKES],
        dt.datetime(2026, 7, 17, 0, 5, tzinfo=dt.UTC),
    )
    result = module.replay_symbol("ES", settings, oi_repo, create_engine(url))
    assert [day for day, _ in result.unverifiable] == [TODAY]
    assert result.new_alpha is None and not result.validation
