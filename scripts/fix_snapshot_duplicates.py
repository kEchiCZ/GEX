"""Jednorázová oprava snapshot partic s duplicitními řádky (#1047).

Do partice `snapshots/{sym}/{expiry}/{den}.parquet` zapisoval IBKR řetěz i
extended tasty větev bez upsertu, takže minuta předání (připojení IBKR) nesla
dva řádky téhož `ts_min × strike × right` a pivot heatmapy v API padal na
„Index contains duplicate entries". Engine po opravě zápisu duplicity
neprodukuje a dnešní partici si po restartu opraví sám; tenhle skript srovná
HISTORICKÉ partice, kterých se už žádný zápis nedotkne.

Duplicitu rozhoduje `parquet_store.dedupe_last` — stejně jako engine při
načtení: vítězí poslední výskyt (pozdější zápis, řádek IBKR řetězu s volume).
Hodnoty řádků se nemění.

Bez `--apply` jen vypíše, co by udělal. Spouštět při ZASTAVENÉM enginu —
runtime partice dnešního dne přepisuje z paměti a souběžný zápis by opravu
přemazal.

    docker compose run --rm engine python scripts/fix_snapshot_duplicates.py            # dry-run
    docker compose run --rm engine python scripts/fix_snapshot_duplicates.py --apply
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Iterable
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from gexlens_engine.config import Settings
from gexlens_engine.storage.parquet_store import (
    SNAPSHOT_KEY,
    SNAPSHOT_SCHEMA,
    dedupe_last,
    key_getter,
)

logger = logging.getLogger("fix_snapshot_duplicates")


def _write_partition(path: Path, rows: list[dict[str, object]]) -> None:
    """Atomický zápis stejně jako `_PartitionBuffer._write` (tmp + os.replace)."""
    rows.sort(key=key_getter(SNAPSHOT_KEY))
    table = pa.Table.from_pylist(rows, schema=SNAPSHOT_SCHEMA)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    pq.write_table(table, tmp_path)
    os.replace(tmp_path, path)


def fix_partition(path: Path, *, apply: bool) -> int:
    """Vrátí počet zahozených duplicit (0 = partice v pořádku)."""
    rows = pq.read_table(path, schema=SNAPSHOT_SCHEMA).to_pylist()
    kept, dropped = dedupe_last(rows, SNAPSHOT_KEY)
    if not dropped:
        return 0
    # Minuty, ve kterých se zapisovači potkali — z logu má být vidět, že jde
    # o minutu předání (připojení IBKR), ne o rozsypanou partici
    before: dict[object, int] = {}
    for row in rows:
        before[row["ts_min"]] = before.get(row["ts_min"], 0) + 1
    after: dict[object, int] = {}
    for row in kept:
        after[row["ts_min"]] = after.get(row["ts_min"], 0) + 1
    affected = [str(ts) for ts, count in before.items() if count != after.get(ts, 0)]
    logger.info(
        "  %s: %d → %d řádků (−%d), minuty s duplicitou: %s (partice %d minut)",
        path,
        len(rows),
        len(kept),
        dropped,
        ", ".join(affected),
        len(before),
    )
    if apply:
        _write_partition(path, kept)
    return dropped


def fix_all(snapshots_dir: Path, *, apply: bool, symbols: Iterable[str] | None) -> int:
    chosen = (
        list(symbols) if symbols else sorted(p.name for p in snapshots_dir.iterdir() if p.is_dir())
    )
    total_dropped = 0
    fixed_files = 0
    scanned = 0
    for symbol in chosen:
        for path in sorted((snapshots_dir / symbol).glob("*/*.parquet")):
            scanned += 1
            dropped = fix_partition(path, apply=apply)
            if dropped:
                fixed_files += 1
                total_dropped += dropped
    logger.info(
        "%d partic prohlédnuto, %d s duplicitou, %d řádků %s",
        scanned,
        fixed_files,
        total_dropped,
        "zahozeno" if apply else "k zahození",
    )
    return fixed_files


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="skutečně přepsat partice")
    parser.add_argument("--data-dir", type=Path, default=None, help="výchozí GEXLENS_DATA_DIR")
    parser.add_argument("--symbols", nargs="*", default=None, help="výchozí všechny v snapshots/")
    args = parser.parse_args(list(argv) if argv is not None else None)

    snapshots_dir = (args.data_dir or Settings().data_dir) / "snapshots"
    if not snapshots_dir.is_dir():
        logger.error("V %s nejsou žádné snapshot partice", snapshots_dir)
        return 2
    if not args.apply:
        logger.info("DRY-RUN — nic se nepřepisuje (spusť s --apply)")
    fix_all(snapshots_dir, apply=args.apply, symbols=args.symbols)
    return 0


if __name__ == "__main__":
    sys.exit(main())
