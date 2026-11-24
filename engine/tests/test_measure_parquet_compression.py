"""Měření komprese parquetů `scripts/measure_parquet_compression.py` nad fixture v tmp (#1855).

Ověřuje, že měření samo nelže: bezztrátovost odhalí změnu pořadí řádků, typu
i hodnoty a NaN na stejném místě bere jako shodu; smíšený adresář snappy + zstd
se čte beze změny čteček; výstup se nesmí překrývat se vstupem (vstup = kopie
vzorku, jen se čte).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "measure_parquet_compression.py"
DAYS = ("2026-11-19", "2026-11-20")


@pytest.fixture(scope="module")
def mod() -> Any:
    spec = importlib.util.spec_from_file_location("measure_parquet_compression", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def snapshot_table(rows: int, seed: float) -> pa.Table:
    start = dt.datetime(2026, 11, 20, 14, 30, tzinfo=dt.UTC)
    ts = [start + dt.timedelta(minutes=i // 10) for i in range(rows)]
    strike = [6600.0 + 5 * (i % 10) for i in range(rows)]
    # greeks = vysoká kardinalita (kandidát BSS), NaN a null jako v reálných datech
    delta: list[float | None] = [((i * 7919) % 1000) / 1000 + seed for i in range(rows)]
    delta[3] = float("nan")
    delta[5] = None
    return pa.table(
        {
            "ts_min": pa.array(ts, type=pa.timestamp("us", tz="UTC")),
            "strike": pa.array(strike),
            "right": pa.array(["C" if i % 2 else "P" for i in range(rows)]),
            "delta": pa.array(delta, type=pa.float64()),
        }
    )


@pytest.fixture()
def sample(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    for index, day in enumerate(DAYS):
        snap = data / "snapshots" / "ES" / "20261120" / f"{day}.parquet"
        bars = data / "derived" / "ES" / "bars" / f"{day}.parquet"
        levels = data / "derived" / "ES" / "20261120" / "levels" / f"{day}.parquet"
        for path in (snap, bars, levels):
            path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(snapshot_table(200 + 100 * index, index / 10), snap)
        pq.write_table(pa.table({"ts_min": [index], "close": [6610.25 + index]}), bars)
        pq.write_table(pa.table({"ts_min": [index], "zero_gamma": [None]}), levels)
    return data


def test_tables_identical_nan_order_and_types(mod: Any) -> None:
    table = snapshot_table(50, 0.0)
    assert mod.tables_identical(table, pa.Table.from_batches(table.to_batches(max_chunksize=7)))
    reordered = table.take(pa.array(list(reversed(range(table.num_rows)))))
    assert not mod.tables_identical(table, reordered)
    retyped = table.set_column(1, "strike", table.column("strike").cast(pa.float32()))
    assert not mod.tables_identical(table, retyped)
    values = table.column("delta").to_pylist()
    values[10] = values[10] + 1e-12
    changed = table.set_column(3, "delta", pa.array(values, type=pa.float64()))
    assert not mod.tables_identical(table, changed)


def test_bss_columns_pick_high_cardinality_floats(mod: Any) -> None:
    assert mod.bss_columns(snapshot_table(200, 0.0)) == ["delta"]
    assert mod.bss_columns(snapshot_table(mod.BSS_MIN_ROWS - 1, 0.0)) == []
    kwargs, split = mod.write_kwargs("zstd3-bss", snapshot_table(200, 0.0))
    assert split == ["delta"]
    assert kwargs["use_byte_stream_split"] == ["delta"]
    assert "delta" not in kwargs["use_dictionary"]
    assert mod.write_kwargs("zstd1", snapshot_table(200, 0.0)) == (
        {"compression": "zstd", "compression_level": 1},
        [],
    )


@pytest.mark.parametrize("name", ["lz4", "snappy-bss", "zstd", "zstd3-x"])
def test_unknown_variant_fails_fast(mod: Any, name: str) -> None:
    with pytest.raises(mod.MeasureError):
        mod.variant_options(name)


def test_partition_kind(mod: Any) -> None:
    assert mod.partition_kind(Path("snapshots/ES/20261120/2026-11-20.parquet")) == "snapshots"
    assert mod.partition_kind(Path("derived/ES/bars/2026-11-20.parquet")) == "derived/bars"
    assert (
        mod.partition_kind(Path("derived/ES/20261120/levels/2026-11-20.parquet"))
        == "derived/levels"
    )
    assert mod.partition_kind(Path("trades/ES/2026-11-20.parquet")) == "trades"


def test_full_run_sizes_write_read_mixed(mod: Any, sample: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    report_path = tmp_path / "report.json"
    before = {p: p.read_bytes() for p in sample.rglob("*.parquet")}
    code = mod.main(
        [
            "--data",
            str(sample),
            "--out",
            str(out),
            "--variants",
            "snappy,zstd3,zstd3-bss",
            "--repeat",
            "1",
            "--report",
            str(report_path),
        ]
    )
    assert code == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    sizes = report["sizes"]["variants"]
    assert all(v["lossless"] for v in sizes.values())
    assert sizes["zstd3-bss"]["bss_columns"] == {"snapshots": ["delta"]}
    assert set(sizes["snappy"]["by_kind"]) == {"derived/bars", "derived/levels", "snapshots"}
    assert report["write"]["day"] == "2026-11-20"  # větší den = nejhorší případ
    assert report["write"]["files"] == 3
    assert set(report["read"]) == {"original", "snappy", "zstd3", "zstd3-bss"}
    assert report["mixed"]["ok"] is True
    assert report["mixed"]["dirs_with_two_codecs"] == report["mixed"]["dirs_checked"] == 3
    zstd_file = out / "zstd3" / "data" / "snapshots" / "ES" / "20261120" / "2026-11-20.parquet"
    assert mod.file_codec(zstd_file) == "ZSTD"
    assert not (out / "_writebench").exists()
    # vstup se jen čte
    assert {p: p.read_bytes() for p in sample.rglob("*.parquet")} == before


def test_output_inside_input_is_refused(mod: Any, sample: Path) -> None:
    code = mod.main(["--data", str(sample), "--out", str(sample / "out"), "--phases", "sizes"])
    assert code == 2
    assert not (sample / "out").exists()


def test_read_without_trees_fails_explicitly(mod: Any, sample: Path, tmp_path: Path) -> None:
    code = mod.main(["--data", str(sample), "--out", str(tmp_path / "o"), "--phases", "read"])
    assert code == 2
