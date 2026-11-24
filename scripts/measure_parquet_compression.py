"""Měření komprese parquetů na KOPII vzorku partic (#1855, F23 epiky #1094).

Engine zapisuje partice výchozím pyarrow (snappy, `parquet_store._write`) a každou
minutu přepisuje CELOU denní partici (`runtime.py`: zápis je v cyklu awaitovaný),
takže čas zápisu partice na konci dne přímo prodlužuje minutový cyklus (#1337).
Skript srovná varianty kodeku nad stejnými tabulkami:

- `sizes`: přepíše každý soubor vzorku do `<out>/<varianta>/data/<rel>`, změří
  velikost na disku a ověří bezztrátovost (schéma včetně metadat, typy, pořadí
  řádků a bitová shoda hodnot; NaN na stejném místě je shoda);
- `write`: čas zápisu (tmp + `os.replace` jako engine) každé partice jednoho dne,
  medián z `--repeat`; Σ dne = horní mez zápisu jedné minuty na konci dne;
- `read`: čas čtení stromu varianty přes `pq.read_table` po souborech,
  `pd.read_parquet` (cesta API `DataReader._read`) a `pyarrow.dataset` po adresářích;
- `repo`: odhad místa v repozitáři restic — celé soubory stromu zkomprimované
  zstd úrovně 3 (restic komprimuje sám, takže úspora parquetů je v zálohách menší);
- `mixed`: strom, kde se soubory střídají mezi originálem (snappy) a variantou,
  přečtený beze změny kódu (`pyarrow.dataset`, `read_table` adresáře, pandas)
  a porovnaný s originálem.

Vstupní `--data` se jen čte; výstup musí ležet mimo něj. Produkční `data/` se
nikdy nepředává — jen kopie (vzorek v `D:\\Downloads`, na serveru domovský adresář).
Varianta `zstd3-bss` / `zstd1-bss`: BYTE_STREAM_SPLIT pro float sloupce s vysokou
kardinalitou (greeks), ostatní sloupce slovník; výběr sloupců jde do reportu
(implementace by ho měla jako statický seznam podle schématu, ne výpočtem v cyklu).

Příklad:
    python -I scripts/measure_parquet_compression.py \\
        --data D:/Downloads/gex-komprese-1855/data --out D:/Downloads/gex-komprese-1855 \\
        --report D:/Downloads/gex-komprese-1855/report.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq

PHASES = ("sizes", "repo", "write", "read", "mixed")
DEFAULT_VARIANTS = ("snappy", "zstd1", "zstd3", "zstd9", "zstd1-bss", "zstd3-bss")
DEFAULT_MIXED_VARIANT = "zstd3"
#: Odhad repozitáře restic: formát v2 komprimuje bloky zstd (`--compression auto`,
#: rychlost zhruba úrovně 3); deduplikace se neodhaduje
REPO_ZSTD_LEVEL = 3
#: Float sloupec s podílem různých hodnot nad prahem jde do BYTE_STREAM_SPLIT;
#: pod ním (ceny, striky, OI) vyhrává slovník
BSS_DISTINCT_RATIO = 0.5
#: U malé partice podíl různých hodnot nic neříká (1 řádek = 100 %) a na
#: kódování nezáleží → slovník jako dnes
BSS_MIN_ROWS = 100


class MeasureError(RuntimeError):
    """Chybný vstup měření (cesta, varianta, prázdný vzorek)."""


def variant_options(name: str) -> tuple[dict[str, Any], bool]:
    """Parametry `pq.write_table` varianty a příznak BYTE_STREAM_SPLIT."""
    base, _, suffix = name.partition("-")
    if suffix not in ("", "bss"):
        raise MeasureError(f"neznámá varianta {name!r}")
    if base == "snappy":
        if suffix:
            raise MeasureError("BYTE_STREAM_SPLIT se měří jen se zstd")
        return {}, False  # výchozí pyarrow = dnešní stav enginu
    if base.startswith("zstd") and base[4:].isdigit():
        return {"compression": "zstd", "compression_level": int(base[4:])}, suffix == "bss"
    raise MeasureError(f"neznámá varianta {name!r}")


def bss_columns(table: pa.Table) -> list[str]:
    """Float sloupce s vysokou kardinalitou — kandidáti na BYTE_STREAM_SPLIT."""
    if table.num_rows < BSS_MIN_ROWS:
        return []
    chosen = []
    for field in table.schema:
        if not pa.types.is_floating(field.type):
            continue
        distinct = pc.count_distinct(table.column(field.name)).as_py()
        if distinct / table.num_rows > BSS_DISTINCT_RATIO:
            chosen.append(field.name)
    return chosen


def write_kwargs(name: str, table: pa.Table) -> tuple[dict[str, Any], list[str]]:
    """Úplné parametry zápisu varianty pro konkrétní tabulku (výběr BSS mimo stopky)."""
    options, use_bss = variant_options(name)
    if not use_bss:
        return options, []
    split = bss_columns(table)
    if not split:
        return options, []
    # Slovník má v pyarrow přednost před BYTE_STREAM_SPLIT → u vybraných sloupců vypnout
    dictionary = [column for column in table.column_names if column not in split]
    return {**options, "use_dictionary": dictionary, "use_byte_stream_split": split}, split


def write_partition(table: pa.Table, path: Path, kwargs: dict[str, Any]) -> None:
    """Zápis jako `parquet_store._PartitionBuffer._write`: tmp soubor + atomické `os.replace`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    pq.write_table(table, tmp_path, **kwargs)
    os.replace(tmp_path, path)


def _float_bits(array: pa.Array) -> pa.Array:
    integer = pa.int64() if array.type.bit_width == 64 else pa.int32()
    return pc.fill_null(array.view(integer), 0)


def tables_identical(left: pa.Table, right: pa.Table) -> bool:
    """Bezztrátovost: schéma s metadaty, počet a pořadí řádků, bitová shoda hodnot.

    `Table.equals` bere NaN ≠ NaN, takže float sloupec, který jím neprojde,
    se porovná bitově (maska null zvlášť) — NaN na stejném místě je shoda.
    """
    if not left.schema.equals(right.schema, check_metadata=True):
        return False
    if left.num_rows != right.num_rows:
        return False
    for name in left.column_names:
        a = left.column(name).combine_chunks()
        b = right.column(name).combine_chunks()
        if a.equals(b):
            continue
        if not pa.types.is_floating(a.type):
            return False
        if not a.is_null().equals(b.is_null()):
            return False
        if not _float_bits(a).equals(_float_bits(b)):
            return False
    return True


def partition_kind(rel: Path) -> str:
    """Druh partice pro rozpad velikostí: `snapshots`, `trades`, `derived/<řada>`."""
    top = rel.parts[0]
    if top == "derived" and len(rel.parts) >= 3:
        return f"derived/{rel.parts[-2]}"
    return top


def list_files(root: Path) -> list[Path]:
    files = sorted(p.relative_to(root) for p in root.rglob("*.parquet") if p.is_file())
    if not files:
        raise MeasureError(f"v {root} není žádný parquet")
    return files


def file_codec(path: Path) -> str:
    meta = pq.ParquetFile(path).metadata
    if meta.num_row_groups == 0 or meta.num_columns == 0:
        return "none"
    return str(meta.row_group(0).column(0).compression)


def guard_paths(data: Path, out: Path) -> None:
    data_r, out_r = data.resolve(), out.resolve()
    if out_r == data_r or data_r in out_r.parents or out_r in data_r.parents:
        raise MeasureError(f"výstup {out_r} se nesmí překrývat se vstupem {data_r}")


def measure_sizes(
    data: Path, out: Path, variants: Iterable[str], files: list[Path]
) -> dict[str, Any]:
    """Přepis vzorku do stromu každé varianty + bezztrátovost."""
    baseline_by_kind: dict[str, int] = defaultdict(int)
    for rel in files:
        baseline_by_kind[partition_kind(rel)] += (data / rel).stat().st_size
    baseline_total = sum(baseline_by_kind.values())
    result: dict[str, Any] = {
        "baseline": {"bytes": baseline_total, "by_kind": dict(sorted(baseline_by_kind.items()))},
        "variants": {},
    }
    for name in variants:
        tree = out / name / "data"
        by_kind: dict[str, int] = defaultdict(int)
        mismatches: list[str] = []
        bss_by_kind: dict[str, set[str]] = defaultdict(set)
        started = time.perf_counter()
        for rel in files:
            original = pq.read_table(data / rel)
            kwargs, split = write_kwargs(name, original)
            bss_by_kind[partition_kind(rel)].update(split)
            target = tree / rel
            write_partition(original, target, kwargs)
            by_kind[partition_kind(rel)] += target.stat().st_size
            if not tables_identical(original, pq.read_table(target)):
                mismatches.append(rel.as_posix())
        total = sum(by_kind.values())
        result["variants"][name] = {
            "bytes": total,
            "ratio_vs_baseline": round(total / baseline_total, 4),
            "by_kind": dict(sorted(by_kind.items())),
            "ratio_by_kind": {
                kind: round(size / baseline_by_kind[kind], 4)
                for kind, size in sorted(by_kind.items())
                if baseline_by_kind[kind]
            },
            "lossless": not mismatches,
            "mismatches": mismatches[:20],
            "bss_columns": {k: sorted(v) for k, v in sorted(bss_by_kind.items()) if v},
            "rewrite_s": round(time.perf_counter() - started, 1),
        }
    return result


def pick_write_day(files: list[Path], data: Path) -> str:
    """Den s největším objemem partic (nejhorší případ zápisu na konci dne)."""
    per_day: dict[str, int] = defaultdict(int)
    for rel in files:
        per_day[rel.stem] += (data / rel).stat().st_size
    return max(per_day.items(), key=lambda item: item[1])[0]


def _timed(repeat: int, fn: Callable[..., object], *args: Any) -> float:
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        fn(*args)
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


def measure_write(
    data: Path, out: Path, variants: Iterable[str], files: list[Path], day: str, repeat: int
) -> dict[str, Any]:
    """Σ a maximum času zápisu partic jednoho dne (engine je přepisuje každou minutu).

    V paměti je vždy jen jedna tabulka: soubor po souboru, uvnitř všechny varianty.
    """
    day_files = [rel for rel in files if rel.stem == day]
    if not day_files:
        raise MeasureError(f"vzorek nemá partice dne {day}")
    names = list(variants)
    bench = out / "_writebench"
    per_kind: dict[str, dict[str, float]] = {name: defaultdict(float) for name in names}
    per_file: dict[str, list[tuple[float, str, int]]] = {name: [] for name in names}
    for rel in day_files:
        table = pq.read_table(data / rel)
        for name in names:
            kwargs, _ = write_kwargs(name, table)
            target = bench / name / rel
            seconds = _timed(repeat, write_partition, table, target, kwargs)
            per_kind[name][partition_kind(rel)] += seconds
            per_file[name].append((seconds, rel.as_posix(), target.stat().st_size))
        del table
    result: dict[str, Any] = {
        "day": day,
        "files": len(day_files),
        "baseline_bytes": sum((data / rel).stat().st_size for rel in day_files),
        "variants": {},
    }
    for name in names:
        rows = sorted(per_file[name], reverse=True)
        result["variants"][name] = {
            "sum_ms": round(1000 * sum(s for s, _, _ in rows), 1),
            "max_ms": round(1000 * rows[0][0], 1),
            "max_file": rows[0][1],
            "bytes": sum(size for _, _, size in rows),
            "sum_ms_by_kind": {k: round(1000 * v, 1) for k, v in sorted(per_kind[name].items())},
            "top5_ms": [[round(1000 * s, 1), path, size] for s, path, size in rows[:5]],
        }
    shutil.rmtree(bench, ignore_errors=True)
    return result


# Čtečky drží v paměti vždy jen jeden soubor / dávku; počet řádků brání tomu,
# aby čtení bylo zbytečné, a hlídá, že všechny cesty přečetly totéž.
def _read_tables(paths: list[Path]) -> int:
    return sum(pq.read_table(p).num_rows for p in paths)


def _read_frames(paths: list[Path]) -> int:
    return sum(len(pd.read_parquet(p)) for p in paths)


def _read_datasets(dirs: list[Path]) -> int:
    return sum(
        batch.num_rows for d in dirs for batch in ds.dataset(d, format="parquet").to_batches()
    )


def leaf_dirs(tree: Path, files: list[Path]) -> list[Path]:
    return sorted({(tree / rel).parent for rel in files})


def measure_read(
    data: Path, out: Path, variants: Iterable[str], files: list[Path], repeat: int
) -> dict[str, Any]:
    """Čtení celého stromu varianty třemi cestami (page cache teplá u všech variant)."""
    result: dict[str, Any] = {}
    for name in ("original", *variants):
        tree = data if name == "original" else out / name / "data"
        if not (tree / files[0]).exists():
            raise MeasureError(f"strom varianty {name} chybí — nejdřív fáze sizes")
        paths = [tree / rel for rel in files]
        dirs = leaf_dirs(tree, files)
        rows = {_read_tables(paths), _read_frames(paths), _read_datasets(dirs)}
        if len(rows) != 1:
            raise MeasureError(f"čtečky stromu {name} přečetly různý počet řádků: {rows}")
        result[name] = {
            "rows": rows.pop(),
            "read_table_s": round(_timed(repeat, _read_tables, paths), 2),
            "pandas_s": round(_timed(repeat, _read_frames, paths), 2),
            "dataset_s": round(_timed(repeat, _read_datasets, dirs), 2),
        }
    return result


def measure_repo(
    data: Path, out: Path, variants: Iterable[str], files: list[Path]
) -> dict[str, Any]:
    """Velikost stromu po zstd celých souborů — horní odhad bloků v repozitáři restic."""
    codec = pa.Codec("zstd", compression_level=REPO_ZSTD_LEVEL)
    result: dict[str, Any] = {"zstd_level": REPO_ZSTD_LEVEL}
    for name in ("original", *variants):
        tree = data if name == "original" else out / name / "data"
        if not (tree / files[0]).exists():
            raise MeasureError(f"strom varianty {name} chybí — nejdřív fáze sizes")
        raw = packed = 0
        for rel in files:
            content = (tree / rel).read_bytes()
            raw += len(content)
            packed += codec.compress(content).size
        result[name] = {"bytes": raw, "repo_bytes": packed, "repo_ratio": round(packed / raw, 4)}
    return result


def build_mixed(data: Path, out: Path, variant: str, files: list[Path]) -> Path:
    """Strom, kde se soubory po řadě střídají: sudé originál, liché varianta."""
    source = out / variant / "data"
    mixed = out / "mixed" / "data"
    for index, rel in enumerate(files):
        target = mixed / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2((data if index % 2 == 0 else source) / rel, target)
    return mixed


def check_mixed(data: Path, mixed: Path, files: list[Path]) -> dict[str, Any]:
    """Smíšené kodeky v jednom adresáři čtou dnešní čtečky beze změny kódu.

    Po souborech `pq.read_table` a `pd.read_parquet` (cesta backtestu a API), fragmenty
    jednoho `pyarrow.dataset` nad celým adresářem a jeho průchod po dávkách; v paměti
    je vždy jen jeden soubor.
    """
    checked = 0
    mixed_dirs = 0
    failures: list[str] = []
    for rel_dir in sorted({rel.parent for rel in files}):
        names = sorted(rel.name for rel in files if rel.parent == rel_dir)
        if len({file_codec(mixed / rel_dir / n) for n in names}) > 1:
            mixed_dirs += 1
        dataset = ds.dataset(mixed / rel_dir, format="parquet")
        fragments = {Path(f.path).name: f for f in dataset.get_fragments()}
        if sorted(fragments) != names:
            failures.append(f"{rel_dir.as_posix()} (dataset: soubory)")
            continue
        expected_rows = 0
        for name in names:
            expected = pq.read_table(data / rel_dir / name)
            expected_rows += expected.num_rows
            if not tables_identical(expected, pq.read_table(mixed / rel_dir / name)):
                failures.append(f"{rel_dir.as_posix()}/{name} (read_table)")
            if not tables_identical(expected, fragments[name].to_table()):
                failures.append(f"{rel_dir.as_posix()}/{name} (dataset)")
            original = pd.read_parquet(data / rel_dir / name)
            if not original.equals(pd.read_parquet(mixed / rel_dir / name)):
                failures.append(f"{rel_dir.as_posix()}/{name} (pandas)")
        scanned = sum(batch.num_rows for batch in dataset.to_batches())
        if scanned != expected_rows:
            failures.append(f"{rel_dir.as_posix()} (dataset: {scanned} ≠ {expected_rows} řádků)")
        checked += 1
    return {
        "dirs_checked": checked,
        "dirs_with_two_codecs": mixed_dirs,
        "ok": not failures,
        "failures": failures[:20],
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    data = Path(args.data)
    out = Path(args.out)
    if not data.is_dir():
        raise MeasureError(f"vstup {data} neexistuje")
    guard_paths(data, out)
    variants = [v for v in args.variants.split(",") if v]
    for name in variants:
        variant_options(name)  # neznámá varianta = chyba hned, ne po hodině měření
    phases = [p for p in args.phases.split(",") if p]
    unknown = set(phases) - set(PHASES)
    if unknown:
        raise MeasureError(f"neznámé fáze {sorted(unknown)}")
    files = list_files(data)
    report: dict[str, Any] = {
        "pyarrow": pa.__version__,
        "machine": platform.machine(),
        "files": len(files),
        "days": sorted({rel.stem for rel in files}),
    }
    if "sizes" in phases:
        report["sizes"] = measure_sizes(data, out, variants, files)
    if "repo" in phases:
        report["repo"] = measure_repo(data, out, variants, files)
    if "write" in phases:
        day = args.write_day or pick_write_day(files, data)
        report["write"] = measure_write(data, out, variants, files, day, args.repeat)
    if "read" in phases:
        report["read"] = measure_read(data, out, variants, files, args.repeat)
    if "mixed" in phases:
        if args.mixed_variant not in variants:
            raise MeasureError(f"smíšený strom potřebuje {args.mixed_variant} ve --variants")
        mixed = build_mixed(data, out, args.mixed_variant, files)
        report["mixed"] = {"variant": args.mixed_variant, **check_mixed(data, mixed, files)}
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--data", required=True, help="kořen KOPIE vzorku (snapshots/, derived/…)")
    parser.add_argument("--out", required=True, help="výstupní adresář mimo --data")
    parser.add_argument("--variants", default=",".join(DEFAULT_VARIANTS))
    parser.add_argument("--phases", default=",".join(PHASES))
    parser.add_argument("--write-day", default="", help="den partic pro měření zápisu (YYYY-MM-DD)")
    parser.add_argument("--mixed-variant", default=DEFAULT_MIXED_VARIANT)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--report", default="", help="cesta JSON reportu (jinak stdout)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = run(args)
    except MeasureError as exc:
        print(f"chyba: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
        print(f"report: {args.report}")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
