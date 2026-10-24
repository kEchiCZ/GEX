"""Zpětný dopočet ρ FA (#1665, ADR-0061 bod 5) — `scripts/recompute_fa_rho.py`.

Skript žije ve `scripts/`, načítá se přes importlib (vzor test_recompute_fa).
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
from sqlalchemy import create_engine, select

from gexlens_engine.compute.cumdelta import SOURCE_MIDPOINT
from gexlens_engine.compute.facalibration import AlphaCalibrationPoint
from gexlens_engine.compute.marketclock import daily_close_utc
from gexlens_engine.compute.settle import session_bounds
from gexlens_engine.config import Settings
from gexlens_engine.storage.fa_calibration import (
    SOURCE_VOLUME,
    FaAlphaRepository,
    fa_alpha_history_table,
)
from gexlens_engine.storage.oi_archive import OIEodRepository, OIRecord

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "recompute_fa_rho.py"
TUESDAY, WEDNESDAY, THURSDAY = dt.date(2026, 8, 4), dt.date(2026, 8, 5), dt.date(2026, 8, 6)
STRIKES = [7500.0 + 10 * i for i in range(6)]
MIN = dt.timedelta(minutes=1)


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("recompute_fa_rho", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _snapshots(settings: Settings, expiry: str, session: dt.date, end: dt.datetime) -> None:
    """Snímky řetězu každou minutu seance do `end`; kumulativ call stran roste ke 100 v řezu."""
    start, cutoff = session_bounds(session)[0], daily_close_utc(session)
    total = (cutoff - start) / MIN
    by_day: dict[dt.date, list[tuple[dt.datetime, float, float]]] = {}
    moment = start
    while moment <= end:
        for strike in STRIKES:
            by_day.setdefault(moment.date(), []).append(
                (moment, strike, 100.0 * ((moment - start) / MIN) / total)
            )
        moment += MIN
    for day, rows in by_day.items():
        path = settings.snapshots_dir / "ES" / expiry / f"{day.isoformat()}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.table(
                {
                    "ts_min": pa.array([r[0] for r in rows], pa.timestamp("us", tz="UTC")),
                    "strike": pa.array([r[1] for r in rows], pa.float64()),
                    "right": pa.array(["C"] * len(rows), pa.string()),
                    "volume": pa.array([r[2] for r in rows], pa.float64()),
                }
            ),
            path,
        )


def _seed(tmp_path: Path) -> tuple[str, Settings]:
    """Úterý 4. 8. platné (ΔOI 40 na V 100 → 0,4), středa 5. 8. useknutá ve 14:18 UTC.

    Produkce nese bod α z midpointu za úterý — `--apply` ho přepíše bodem ρ.
    """
    settings = Settings(data_dir=tmp_path / "data")
    url = f"sqlite+pysqlite:///{tmp_path / 'db.sqlite'}"
    db = create_engine(url)
    oi_repo = OIEodRepository(db)
    oi_repo.ensure_schema()
    alpha = FaAlphaRepository(db)
    alpha.ensure_schema()
    for session, expiry, end in (
        (TUESDAY, "20260805", daily_close_utc(TUESDAY)),
        (WEDNESDAY, "20260806", dt.datetime(2026, 8, 5, 14, 18, tzinfo=dt.UTC)),
    ):
        _snapshots(settings, expiry, session, end)
        next_day = session + dt.timedelta(days=1)
        for day, oi in ((session, 1000.0), (next_day, 1040.0)):
            oi_repo.upsert_many(
                [OIRecord("ES", expiry, strike, "C", day, oi) for strike in STRIKES],
                settings.oi_publication_utc(day) + dt.timedelta(hours=1),
            )
    alpha.record(
        "ES",
        TUESDAY,
        "20260806",
        AlphaCalibrationPoint(20, -0.1, 0.3, -0.4),
        0.25,
        9,
        source=SOURCE_MIDPOINT,
    )
    alpha.set_state("ES", 0.25, 9, source=SOURCE_MIDPOINT)
    return url, settings


def _history(url: str) -> list[tuple[dt.date, str | None, float, float]]:
    with create_engine(url).connect() as conn:
        return [
            (row.day, row.source, row.ratio_median, row.alpha_after)
            for row in conn.execute(
                select(fa_alpha_history_table).order_by(fa_alpha_history_table.c.day)
            )
        ]


def _run(module: Any, url: str, settings: Settings, out: Path, *extra: str) -> int:
    code: int = module.main(
        [
            "--db",
            url,
            "--data",
            str(settings.data_dir),
            "--symbols",
            "ES",
            "--from",
            TUESDAY.isoformat(),
            "--out",
            str(out),
            *extra,
        ]
    )
    return code


def test_dry_run_report_po_dnech_nic_nezapise(tmp_path: Path) -> None:
    module = _load()
    url, settings = _seed(tmp_path)
    before = _history(url)
    assert _run(module, url, settings, tmp_path / "out") == 0
    assert _history(url) == before  # dry-run nic nezapíše
    state = FaAlphaRepository(create_engine(url)).get("ES")
    assert state is not None and state.source == SOURCE_MIDPOINT

    report = next((tmp_path / "out").glob("recompute-fa-rho-*.md")).read_text(encoding="utf-8")
    assert "ρ po dopočtu: **0.400** (1 dnů)" in report
    assert "| 04. 08. | 20260805 |" in report
    assert "snímky končí 14:18" in report  # useknutá středa (vzor NQ 9. 10.) bod nedá
    assert "2026-08-04: bod zdroje midpoint s mediánem -0.100" in report


def test_apply_zapise_body_a_stav_volume_idempotentne(tmp_path: Path) -> None:
    module = _load()
    url, settings = _seed(tmp_path)
    assert _run(module, url, settings, tmp_path / "out", "--apply", "--yes") == 0
    history = _history(url)
    assert history == [(TUESDAY, SOURCE_VOLUME, pytest.approx(0.4), pytest.approx(0.4))]
    state = FaAlphaRepository(create_engine(url)).get("ES")
    assert state is not None and state.source == SOURCE_VOLUME
    assert (state.alpha, state.days) == (pytest.approx(0.4), 1)

    assert _run(module, url, settings, tmp_path / "out", "--apply", "--yes") == 0
    assert _history(url) == history  # opakovaný běh zapíše totéž


def test_report_nesmi_do_data(tmp_path: Path) -> None:
    module = _load()
    url, settings = _seed(tmp_path)
    with pytest.raises(SystemExit):
        _run(module, url, settings, settings.data_dir / "reports")
