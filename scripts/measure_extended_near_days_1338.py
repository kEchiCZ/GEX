"""Objem snapshotů podle DTE a odhad pro hranici minutového zápisu extended expirací (#1338 D-b).

Jen čtení partic `snapshots/{sym}/{expiry}/{den}.parquet`, nic se nezapisuje. Podklad
ADR-0058 (hranice 7 → 5 dnů) a jeho ověření po nasazení:

- **objem**: MB za seanci celkem, IBKR expirace ES a NQ (řádky s `volume`), extended
  expirace ES a NQ (řádky bez `volume`) a ostatní symboly (ad-hoc pohledy);
- **rozpad extended podle DTE** (expirace − den partice, kalendářní dny jako engine):
  MB za seanci, podíl a počet řádků mimo minuty dělitelné intervalem (po nasazení
  ADR-0058 má být u DTE nad hranicí 0);
- **odhad pro hranici** `--near-days`: řádek zůstane, když by ho engine v té minutě
  zapsal (`tasty/extended.cadence_due`, tatáž čistá funkce, interval z konfigurace),
  bajty úměrně řádkům. Hranice nad tou, se kterou se data sbírala, dá jen dolní mez
  (chybějící minuty nejde dopočítat).

Seance D leží v particích D−1 (večer) a D, takže rozsah má začínat dnem otevření první
seance (pro týden neděle). Počet seancí = obchodní dny v rozsahu
(`compute/settle.is_trading_session`).

Spuštění (z kořene repa; `--data` = kořen dat, výchozí `data`):
    python -I scripts/measure_extended_near_days_1338.py --from 2026-10-25 --to 2026-10-30
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "engine" / "src"))

import pyarrow as pa  # noqa: E402
import pyarrow.compute as pc  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from gexlens_engine.compute.settle import is_trading_session  # noqa: E402
from gexlens_engine.config import Settings  # noqa: E402
from gexlens_engine.tasty.extended import cadence_due  # noqa: E402

#: Symboly se sběrem extended expirací (#616)
EXTENDED_SYMBOLS = ("ES", "NQ")
#: Pásma DTE pro rozpad (včetně krajů)
DTE_BUCKETS = ((0, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, 6), (7, 7), (8, 14), (15, 31))
SESSIONS_PER_YEAR = 250
MB = 1e6


def _days(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + dt.timedelta(days=n) for n in range((end - start).days + 1)]


def _minute_counts(table_ts: pa.ChunkedArray) -> Counter[int]:
    """Počet řádků per minuta dne UTC (`hour·60 + minute` jako engine)."""
    minute = pc.add(pc.multiply(pc.hour(table_ts), 60), pc.minute(table_ts))
    return Counter(minute.to_pylist())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Objem snapshotů podle DTE a odhad hranice extended expirací (#1338)"
    )
    parser.add_argument("--data", type=Path, default=Path("data"), help="kořen dat (snapshots/)")
    parser.add_argument("--from", dest="start", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--to", dest="end", type=dt.date.fromisoformat, required=True)
    parser.add_argument(
        "--near-days", type=int, nargs="+", default=[3, 4, 5, 6, 7], help="hranice k odhadu"
    )
    args = parser.parse_args()
    root: Path = args.data / "snapshots"
    if not root.is_dir():
        print(f"Adresář {root} neexistuje", file=sys.stderr)
        return 2
    if args.end < args.start:
        print("--to musí být ≥ --from", file=sys.stderr)
        return 2
    days = _days(args.start, args.end)
    sessions = sum(is_trading_session(day) for day in days)
    if sessions == 0:
        print("Rozsah nemá žádnou obchodní seanci", file=sys.stderr)
        return 2
    far = Settings.model_fields["tasty_extended_far_interval_min"].default

    total = ibkr = other = 0.0
    by_dte: defaultdict[int, float] = defaultdict(float)
    off_cadence: defaultdict[int, int] = defaultdict(int)
    estimate = dict.fromkeys(args.near_days, 0.0)
    for sym_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for exp_dir in sorted(p for p in sym_dir.iterdir() if p.is_dir()):
            try:
                expiry_date = dt.datetime.strptime(exp_dir.name, "%Y%m%d").date()
            except ValueError:
                continue
            for day in days:
                path = exp_dir / f"{day.isoformat()}.parquet"
                if not path.exists():
                    continue
                size = path.stat().st_size
                total += size
                if sym_dir.name not in EXTENDED_SYMBOLS:
                    other += size
                    continue
                table = pq.read_table(path, columns=["ts_min", "volume"])
                if table.num_rows == 0:
                    continue
                per_row = size / table.num_rows
                has_volume = pc.is_valid(table.column("volume"))
                ibkr += (pc.sum(has_volume).as_py() or 0) * per_row
                extended = table.filter(pc.invert(has_volume))
                if extended.num_rows == 0:
                    continue
                dte = (expiry_date - day).days
                by_dte[dte] += extended.num_rows * per_row
                counts = _minute_counts(extended.column("ts_min"))
                off_cadence[dte] += sum(n for minute, n in counts.items() if minute % far)
                for near_days in estimate:
                    kept = sum(
                        n
                        for minute, n in counts.items()
                        if cadence_due(
                            exp_dir.name,
                            today=day,
                            minute_of_day=minute,
                            near_days=near_days,
                            far_interval_min=far,
                        )
                    )
                    estimate[near_days] += kept * per_row

    if total == 0:
        print(f"V {root} nejsou partice za {args.start} až {args.end}", file=sys.stderr)
        return 1
    extended_total = sum(by_dte.values())
    print(f"Rozsah {args.start} až {args.end}: {len(days)} partic dne, {sessions} seancí")
    print(
        f"snapshots celkem {total / MB / sessions:.0f} MB/seanci; "
        f"ES+NQ IBKR {ibkr / MB / sessions:.0f}, ES+NQ extended "
        f"{extended_total / MB / sessions:.0f}, ostatní symboly {other / MB / sessions:.0f}"
    )
    for low, high in DTE_BUCKETS:
        size = sum(v for k, v in by_dte.items() if low <= k <= high)
        off = sum(v for k, v in off_cadence.items() if low <= k <= high)
        label = f"{low}" if low == high else f"{low}-{high}"
        print(
            f"extended DTE {label:>5}: {size / MB / sessions:6.1f} MB/seanci "
            f"({size / total * 100:4.1f} % snapshots), řádků mimo minuty dělitelné {far}: {off}"
        )
    for near_days in sorted(estimate):
        new_total = total - extended_total + estimate[near_days]
        print(
            f"near_days={near_days}: {new_total / MB / sessions:6.1f} MB/seanci, "
            f"úspora {(1 - new_total / total) * 100:4.1f} %, "
            f"~{new_total / MB / sessions * SESSIONS_PER_YEAR / 1000:4.1f} GB/rok"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
