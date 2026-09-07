"""Jednorázová oprava snapshot partic s duplicitními řádky (#1047).

Do partice `snapshots/{sym}/{expiry}/{den}.parquet` zapisovalo víc zapisovačů
bez upsertu — IBKR řetěz i extended tasty větev v minutě předání, a starý i
nový proces enginu v minutě restartu (catch-up sweep zapsal minutu, kterou
předchozí proces stihl zapsat před ukončením). Duplicitní `ts_min × strike ×
right` shodí pivot heatmapy v API („Index contains duplicate entries") a den
se v replayi nedá otevřít. Sken 7. 9. 2026 na prod: 50 z 586 partic od července.

Engine po opravě zápisu duplicity neprodukuje a partice, které drží v paměti,
si po restartu opraví sám; tenhle skript srovná HISTORICKÉ partice, kterých se
už žádný zápis nedotkne.

Duplicitu rozhoduje stejné pravidlo jako engine při načtení
(`parquet_store.dedupe_last`): vítězí poslední výskyt = pozdější zápis.
Hodnoty řádků se nemění.

Bez `--apply` jen vypíše, co by udělal. Partice dnů, které běžící engine drží
v paměti (od jeho startu), by souběžný zápis přemazal — proto `--until DEN`
(exkluzivně) omezí opravu na starší dny a dá se pustit za běhu; bez `--until`
spouštět jen při ZASTAVENÉM enginu.

    docker compose run --rm engine \
        python scripts/fix_snapshot_duplicates.py                            # dry-run
    docker compose run --rm engine \
        python scripts/fix_snapshot_duplicates.py --until 2026-09-07 --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from collections.abc import Iterable
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from gexlens_engine.config import Settings
from gexlens_engine.storage.parquet_store import SNAPSHOT_KEY, SNAPSHOT_SCHEMA

logger = logging.getLogger("fix_snapshot_duplicates")

KEY = list(SNAPSHOT_KEY)


def _write_partition(path: Path, frame: pd.DataFrame) -> None:
    """Atomický zápis stejně jako `_PartitionBuffer._write` (tmp + os.replace)."""
    table = pa.Table.from_pandas(frame, schema=SNAPSHOT_SCHEMA, preserve_index=False)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    pq.write_table(table, tmp_path)
    os.replace(tmp_path, path)


def fix_partition(path: Path, *, apply: bool) -> int:
    """Vrátí počet zahozených duplicit (0 = partice v pořádku).

    Pandas místo `to_pylist`: sken 586 partic přes seznam slovníků trval 25 min,
    přes DataFrame vteřiny — a detekce beze změny obsahu je většinový případ.
    """
    frame = pq.read_table(path, schema=SNAPSHOT_SCHEMA).to_pandas()
    duplicate_mask = frame.duplicated(KEY, keep="last")
    dropped = int(duplicate_mask.sum())
    if not dropped:
        return 0
    affected = sorted(str(ts) for ts in frame.loc[duplicate_mask, "ts_min"].unique())
    logger.info(
        "  %s: %d → %d řádků (−%d), minuty s duplicitou: %s (partice %d minut)",
        path,
        len(frame),
        len(frame) - dropped,
        dropped,
        ", ".join(affected),
        frame["ts_min"].nunique(),
    )
    if apply:
        kept = frame.loc[~duplicate_mask].sort_values(KEY, kind="stable")
        _write_partition(path, kept)
    return dropped


def _partition_day(path: Path) -> dt.date | None:
    try:
        return dt.date.fromisoformat(path.stem)
    except ValueError:
        logger.warning("Partice s nečitelným datem, přeskakuji: %s", path)
        return None


def fix_all(
    snapshots_dir: Path,
    *,
    apply: bool,
    symbols: Iterable[str] | None,
    until: dt.date | None,
) -> int:
    chosen = (
        list(symbols) if symbols else sorted(p.name for p in snapshots_dir.iterdir() if p.is_dir())
    )
    total_dropped = 0
    fixed_files = 0
    scanned = 0
    for symbol in chosen:
        for path in sorted((snapshots_dir / symbol).glob("*/*.parquet")):
            day = _partition_day(path)
            if day is None or (until is not None and day >= until):
                continue
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
    parser.add_argument(
        "--until",
        type=dt.date.fromisoformat,
        default=None,
        help="opravit jen partice dnů PŘED tímto dnem (YYYY-MM-DD) — bezpečné za běhu enginu",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    snapshots_dir = (args.data_dir or Settings().data_dir) / "snapshots"
    if not snapshots_dir.is_dir():
        logger.error("V %s nejsou žádné snapshot partice", snapshots_dir)
        return 2
    if not args.apply:
        logger.info("DRY-RUN — nic se nepřepisuje (spusť s --apply)")
    elif args.until is None:
        logger.warning("Bez --until přepisuji i dnešní partice — engine musí být ZASTAVENÝ")
    fix_all(snapshots_dir, apply=args.apply, symbols=args.symbols, until=args.until)
    return 0


if __name__ == "__main__":
    sys.exit(main())
