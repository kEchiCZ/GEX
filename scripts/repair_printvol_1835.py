"""Oprava řady `printvol` po startu enginu s neměřeným objemem jako 0,0 (#1835).

Do opravy `78604a4` adaptér IBKR převáděl `ticker.volume` NaN na 0,0. Čerstvý
ticker ib_async nese NaN, dokud nedorazí tick objemu, a to nastává jen při první
subskripci kontraktu po startu procesu. Ve snímku catch-upu po startu tak kontrakt
s denním kumulativem nesl `volume` 0, tracker CumΔ vzal 0 jako referenci a první
další bar zapsal do `printvol` celý kumulativ jako přírůstek minuty (ES 17. 11.
18:13 → 18:15: 38 129 místo ~1 085).

Oprava = co by zapsal engine s opravou: neměřený objem (`None`) tracker přeskočí
a první měřený kumulativ jen založí stav, takže řádek se nezapíše. Skript proto
**smaže** duchové řádky, nic nepřepočítává a nic nepřidává. Duchový řádek je
první řádek kontraktu v `printvol` po minutě catch-upu, když platí všechno:

- ve snímku catch-upu má kontrakt `volume` 0,0;
- v posledním snímku před catch-upem nesl kumulativ > 0 (nula je tedy neměřená,
  ne skutečná);
- `volume_delta` řádku se rovná kumulativu kontraktu ve snímku téže minuty
  (přírůstek proti referenci 0).

Řádek, který podmínky splní jen zčásti, skript jen vypíše a nechá. Setupy
a tendence mají vlastní `SessionVolumes` s časovou dírou (#1774) a duchový
přírůstek nedostaly (`features` 17. 11. 18:13–18:16 `opt_vol` 0), FA bere
maximum seance. Oprava se jich netýká.

Bez `--apply` jen vypíše, co by udělal (nic nezapisuje). S `--apply` zálohuje
partici do `--backup-dir` (výchozí `data/backup/printvol-1835`) a zapíše novou
přes dočasný soubor a `os.replace`. Opakované spuštění je bezpečné, protože
smazaný řádek už podmínky nesplní. Partice minulého dne engine nepřepisuje
(zapisovač drží v paměti jen aktuální den).

Spuštění z kořene repa:
    python -I -X utf8 scripts/repair_printvol_1835.py --data data --symbol ES \
        --expiry 20261117 --day 2026-11-17 --catchup 18:13 [--apply]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

Contract = tuple[float, str]

#: Tolerance shody přírůstku s kumulativem (objem je celé číslo kontraktů)
MATCH_TOLERANCE = 0.5


def _hm(value: str) -> dt.time:
    return dt.time.fromisoformat(value)


def _volumes(path: Path) -> dict[dt.datetime, dict[Contract, float | None]]:
    """Kumulativ per minuta a kontrakt ze snímků; víc řádků v minutě = poslední."""
    table = pq.read_table(path, columns=["ts_min", "strike", "right", "volume"])
    out: dict[dt.datetime, dict[Contract, float | None]] = defaultdict(dict)
    for row in table.to_pylist():
        out[row["ts_min"]][(row["strike"], row["right"])] = row["volume"]
    return out


def find_ghost_rows(
    snapshots: dict[dt.datetime, dict[Contract, float | None]],
    printvol: list[dict[str, object]],
    catchup: dt.datetime,
) -> tuple[list[int], list[str]]:
    """Indexy duchových řádků `printvol` a popisy kandidátů, kteří podmínky nesplnili."""
    before = sorted(ts for ts in snapshots if ts < catchup)
    if catchup not in snapshots or not before:
        raise SystemExit(f"Snímek catch-upu {catchup:%H:%M} nebo snímek před ním chybí")
    # Poslední známý kumulativ kontraktu před startem (křídla nemusí být v každé minutě)
    previous: dict[Contract, float] = {}
    for ts in before:
        for key, volume in snapshots[ts].items():
            if volume is not None:
                previous[key] = volume
    suspects = {
        key
        for key, volume in snapshots[catchup].items()
        if volume == 0.0 and (previous.get(key) or 0.0) > 0.0
    }
    first_after: dict[Contract, int] = {}
    for index, row in enumerate(printvol):
        key = (row["strike"], row["right"])
        ts = row["ts_min"]
        if key in suspects and isinstance(ts, dt.datetime) and ts > catchup:
            current = first_after.get(key)
            if current is None or ts < printvol[current]["ts_min"]:  # type: ignore[operator]
                first_after[key] = index
    ghosts: list[int] = []
    skipped: list[str] = []
    for key in sorted(suspects):
        index = first_after.get(key)
        if index is None:
            skipped.append(f"{key[0]:g} {key[1]}: bez řádku po catch-upu")
            continue
        row = printvol[index]
        ts = row["ts_min"]
        delta = float(row["volume_delta"])  # type: ignore[arg-type]
        cumulative = snapshots.get(ts, {}).get(key)  # type: ignore[arg-type]
        if cumulative is not None and abs(delta - cumulative) <= MATCH_TOLERANCE:
            ghosts.append(index)
        else:
            skipped.append(
                f"{key[0]:g} {key[1]} {ts:%H:%M}: volume_delta {delta:,.0f}"
                f" ≠ kumulativ {cumulative}"
            )
    return sorted(ghosts), skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data", type=Path, default=Path("data"), help="kořen dat")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--expiry", required=True, help="YYYYMMDD")
    parser.add_argument("--day", type=dt.date.fromisoformat, required=True, help="UTC den partice")
    parser.add_argument("--catchup", type=_hm, required=True, help="HH:MM UTC minuty catch-upu")
    parser.add_argument("--apply", action="store_true", help="zapsat (jinak jen výpis)")
    parser.add_argument("--backup-dir", type=Path, default=None)
    args = parser.parse_args()

    day = args.day.isoformat()
    snap_path = args.data / "snapshots" / args.symbol / args.expiry / f"{day}.parquet"
    pv_path = args.data / "derived" / args.symbol / args.expiry / "printvol" / f"{day}.parquet"
    catchup = dt.datetime.combine(args.day, args.catchup, tzinfo=dt.UTC)

    snapshots = _volumes(snap_path)
    table = pq.read_table(pv_path)
    rows = table.to_pylist()
    ghosts, skipped = find_ghost_rows(snapshots, rows, catchup)

    by_minute: dict[str, list[float]] = defaultdict(list)
    for index in ghosts:
        by_minute[f"{rows[index]['ts_min']:%H:%M}"].append(float(rows[index]["volume_delta"]))
    removed = sum(sum(v) for v in by_minute.values())
    total = pc.sum(table["volume_delta"]).as_py() or 0.0
    print(f"{pv_path}: {len(rows)} řádků, Σ volume_delta {total:,.0f}")
    print(f"Catch-up {catchup:%H:%M}: duchových řádků {len(ghosts)}, Σ {removed:,.0f}")
    for minute, deltas in sorted(by_minute.items()):
        print(f"  {minute}: {len(deltas)} řádků, Σ {sum(deltas):,.0f}")
    for line in skipped:
        print(f"  nechávám: {line}")
    print(f"Po opravě: {len(rows) - len(ghosts)} řádků, Σ volume_delta {total - removed:,.0f}")

    if not ghosts:
        print("Nic k opravě.")
        return
    if not args.apply:
        print("Jen výpis (bez --apply), nic nezapsáno.")
        return

    backup_dir = args.backup_dir or (args.data / "backup" / "printvol-1835")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{args.symbol}-{args.expiry}-{day}.parquet"
    if not backup.exists():
        shutil.copy2(pv_path, backup)
    keep = pa.array([i not in set(ghosts) for i in range(len(rows))])
    tmp = pv_path.with_suffix(".parquet.tmp")
    pq.write_table(table.filter(keep), tmp)
    os.replace(tmp, pv_path)
    print(f"Zapsáno {pv_path} (záloha {backup})")


if __name__ == "__main__":
    main()
