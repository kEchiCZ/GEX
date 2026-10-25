"""Dopad opravy #1641/#1642 na replay setupů: verze skriptu před × po.

Do #1641 hodnotil `backtest_setups.replay` v rozhodovací minutě N bar N − 1
s razítkem N a okno končilo před minutou settle: bar 19:59 se nehodnotil,
timeout bral close baru 19:58 a `closed` byl o minutu později než živě.
Skript přehraje tytéž uzavřené expirace verzí `scripts/backtest_setups.py`
z revize `--before` a sousední (opravenou) verzí, baseline parametry
(`SetupParams()`), a vypíše per symbol počet setupů, ΣR, timeouty, setupy
bez konce a seznam rozdílů (setup jen před / jen po, změněný výsledek).

Jen čte partice a s `--db` OI archiv pro Max Pain (spojení jen pro čtení).
Plný replay dvou verzí je ~4 min CPU na 25 expirací; pouštět při zavřeném
trhu a po jednom symbolu (měření 25. 10. 2026 nad všemi expiracemi zastavila
paměť hostitele):
    uv run python scripts/measure_replay_bar_1641.py --symbol ES --last 25
        --to 20261023 --before 82bcefc --db "postgresql+psycopg://…@127.0.0.1:55432/gexlens"
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import os
import subprocess
import sys
import tempfile
from types import ModuleType

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_setups as bt  # noqa: E402 — sousední skript, ne balík
from sqlalchemy import create_engine  # noqa: E402

from gexlens_engine.compute.setups import SetupParams  # noqa: E402
from gexlens_engine.storage.oi_archive import OIEodRepository  # noqa: E402

Key = tuple[str, str, dt.datetime]


def load_before(revision: str, folder: str) -> ModuleType:
    """Načte `backtest_setups.py` z revize gitu jako samostatný modul."""
    source = subprocess.run(
        ["git", "show", f"{revision}:scripts/backtest_setups.py"],
        check=True,
        capture_output=True,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    ).stdout
    path = os.path.join(folder, "backtest_setups_before.py")
    with open(path, "wb") as handle:
        handle.write(source)
    spec = importlib.util.spec_from_file_location("backtest_setups_before", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"nelze načíst {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def rows_of(module: ModuleType, symbol: str, expiry: str, repo: object) -> dict[Key, dict]:
    minutes = module.build_minutes(symbol, expiry, repo)
    return {
        (r["template"], r["direction"], r["created"]): r
        for r in module.replay(minutes, SetupParams(), expiry, symbol)
    }


def measure(before: ModuleType, symbol: str, last: int, to: str, repo: object) -> None:
    now = dt.datetime.now(dt.UTC)
    expiries = [
        name
        for name in sorted(os.listdir(f"{bt.DATA}/{symbol}"))
        if name.isdigit() and name <= to and bt.settled(symbol, name, now)
    ][-last:]
    print(f"{symbol}: {len(expiries)} expirací {expiries[0]}–{expiries[-1]}", flush=True)
    tags = ("before", "after")
    totals = {f"{k}_{tag}": 0.0 for k in ("n", "r", "timeout", "open") for tag in tags}
    diffs: list[str] = []
    for expiry in expiries:
        rows = {
            "before": rows_of(before, symbol, expiry, repo),
            "after": rows_of(bt, symbol, expiry, repo),
        }
        for tag, items in rows.items():
            totals[f"n_{tag}"] += len(items)
            totals[f"r_{tag}"] += sum(r["r"] or 0.0 for r in items.values())
            outcomes = [r["outcome"] for r in items.values()]
            totals[f"timeout_{tag}"] += outcomes.count("closed_timeout")
            totals[f"open_{tag}"] += outcomes.count("active")
        for key in sorted(rows["before"].keys() ^ rows["after"].keys()):
            tag = "jen před" if key in rows["before"] else "jen po"
            row = rows["before"].get(key) or rows["after"][key]
            diffs.append(
                f"{expiry} {tag}: {key[0]} {key[1]} {key[2]:%H:%M} {row['outcome']} {row['r']}"
            )
        for key in sorted(rows["before"].keys() & rows["after"].keys()):
            a, b = rows["before"][key], rows["after"][key]
            if a["outcome"] != b["outcome"] or a["r"] != b["r"]:
                diffs.append(
                    f"{expiry} {key[0]} {key[1]} {key[2]:%H:%M} "
                    f"{a['outcome']} {a['r']} → {b['outcome']} {b['r']}"
                )
    print(
        f"{symbol}: setupy {totals['n_before']:.0f} → {totals['n_after']:.0f}, "
        f"ΣR {totals['r_before']:+.2f} → {totals['r_after']:+.2f}, "
        f"timeout {totals['timeout_before']:.0f} → {totals['timeout_after']:.0f}, "
        f"bez konce {totals['open_before']:.0f} → {totals['open_after']:.0f}, "
        f"rozdílů {len(diffs)}"
    )
    for line in diffs:
        print("  ", line)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--last", type=int, default=25)
    parser.add_argument("--to", default="99999999", help="poslední expirace YYYYMMDD")
    parser.add_argument("--before", required=True, help="revize skriptu před opravou")
    parser.add_argument("--data", default=bt.ROOT, help="kořen dat (derived/, snapshots/)")
    parser.add_argument("--db", default="", help="URL PG pro Max Pain; bez ní Max Pain chybí")
    args = parser.parse_args()
    # Windows konzole má cp1250 a na „→" ve výstupu spadne — výstup je vždy UTF-8
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    repo = (
        OIEodRepository(
            create_engine(args.db, connect_args={"options": "-c default_transaction_read_only=on"})
        )
        if args.db
        else None
    )
    with tempfile.TemporaryDirectory() as folder:
        before = load_before(args.before, folder)
        for module in (bt, before):
            module.set_data_root(args.data)
        measure(before, args.symbol, args.last, args.to, repo)


if __name__ == "__main__":
    main()
