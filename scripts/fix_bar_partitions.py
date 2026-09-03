"""Jednorázová oprava partic barů podkladu (#1002).

Přesune bary zapsané do partice cizího dne (půlnoční bar 23:59 v D+1,
rekonstruovaný blok 22:00–23:59 dne D−1 v D) do partice jejich UTC dne a
duplikáty rozhodne podle `storage/bar_partitions.py` (měřený > rekonstruovaný,
větší objem, domácí partice). Hodnoty barů se nemění.

Bez `--apply` jen vypíše, co by udělal. Spouštět při ZASTAVENÉM enginu —
runtime partice přepisuje a souběžný zápis by se ztratil.

    docker compose run --rm engine python scripts/fix_bar_partitions.py            # dry-run
    docker compose run --rm engine python scripts/fix_bar_partitions.py --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from collections.abc import Iterable
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from gexlens_engine.config import Settings
from gexlens_engine.storage.bar_partitions import RepartitionPlan, plan_repartition
from gexlens_engine.storage.parquet_store import BARS_SCHEMA

logger = logging.getLogger("fix_bar_partitions")


def _read_partitions(bars_dir: Path) -> dict[dt.date, list[dict[str, object]]]:
    rows_by_file: dict[dt.date, list[dict[str, object]]] = {}
    for path in sorted(bars_dir.glob("*.parquet")):
        try:
            day = dt.date.fromisoformat(path.stem)
        except ValueError:
            logger.warning("Partice s nečitelným datem, přeskakuji: %s", path)
            continue
        table = pq.read_table(path, schema=BARS_SCHEMA)
        rows_by_file[day] = [row for row in table.to_pylist() if row.get("ts_min") is not None]
    return rows_by_file


def _write_partition(path: Path, rows: list[dict[str, object]]) -> None:
    """Atomický zápis stejně jako `_PartitionBuffer._write` (tmp + os.replace)."""
    table = pa.Table.from_pylist(sorted(rows, key=lambda r: r["ts_min"]), schema=BARS_SCHEMA)  # type: ignore[arg-type, return-value]
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    pq.write_table(table, tmp_path)
    os.replace(tmp_path, path)


def fix_symbol(bars_dir: Path, *, apply: bool) -> RepartitionPlan:
    rows_by_file = _read_partitions(bars_dir)
    plan = plan_repartition(rows_by_file)
    total_before = sum(len(rows) for rows in rows_by_file.values())
    logger.info(
        "%s: %d partic, %d řádků; k opravě %d dnů — přesunuto %d, zahozeno %d, "
        "nahrazeno %d (výsledek −%d řádků)",
        bars_dir.parent.name,
        len(rows_by_file),
        total_before,
        len(plan.changed_days),
        plan.moved,
        plan.dropped,
        plan.replaced,
        plan.dropped + plan.replaced,
    )
    for day in plan.changed_days:
        before = len(rows_by_file.get(day, []))
        after = len(plan.rows_by_day[day])
        logger.info("  %s: %d → %d řádků", day.isoformat(), before, after)
    if apply:
        for day in plan.changed_days:
            rows = plan.rows_by_day[day]
            path = bars_dir / f"{day.isoformat()}.parquet"
            if rows:
                _write_partition(path, rows)
            elif path.exists():
                # partice by zůstala prázdná — to se stane jen, když nesla
                # výhradně cizí bary; prázdný soubor by mátl čtenáře
                path.unlink()
                logger.info("  %s: partice bez vlastních barů smazána", day.isoformat())
    return plan


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="skutečně přepsat partice")
    parser.add_argument("--data-dir", type=Path, default=None, help="výchozí GEXLENS_DATA_DIR")
    parser.add_argument("--symbols", nargs="*", default=None, help="výchozí všechny v derived/")
    args = parser.parse_args(list(argv) if argv is not None else None)

    data_dir = args.data_dir or Settings().data_dir
    derived = data_dir / "derived"
    symbols = args.symbols or sorted(p.name for p in derived.iterdir() if (p / "bars").is_dir())
    if not symbols:
        logger.error("V %s nejsou žádné partice barů", derived)
        return 2
    if not args.apply:
        logger.info("DRY-RUN — nic se nepřepisuje (spusť s --apply)")
    for symbol in symbols:
        fix_symbol(derived / symbol / "bars", apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
