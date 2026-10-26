"""Měření #1609: proběhlo čtení OI z IBKR na začátku UTC dne? (jen čte logy enginu)

Čtení od 00:00 UTC je navržené záměrně (nový den natáhne čítač retry), ale
zkratka „platný snímek dne“ ho přeskočí, když tasty (#828) zapíše řádky dne
dřív. Parquet `oifilled` to neprozradí: fill nesou i striky mimo obálku IBKR,
takže sekundár jede na fallbacku do publikace i ve dnech, kdy IBKR četl.
Stopu nechávají logy enginu: „OI archiv {sym} {den}: … zapsáno“ (do #1674) nebo
„… průchod N kontraktů“ (od #1674) v okně 00:00–00:15 UTC.

Vstup: uložené logy `data/logs/engine-*.log` (deploy je ukládá před recreate)
a volitelně výpis `docker logs gex-engine-1` do souboru MIMO repo (je veřejné).
Den se počítá jen tehdy, když některý log pokrývá celé okno.

    docker logs gex-engine-1 > "$TEMP/engine-now.log" 2>&1
    uv run python scripts/measure_oi_midnight_1609.py data/logs/engine-*.log "$TEMP/engine-now.log"
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from collections import defaultdict
from pathlib import Path

TS = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)")
# Část uložených logů má diakritiku dvojitě kódovanou (`zaps├íno`, `pr┼»chod`)
READ = re.compile(r"OI archiv (ES|NQ) (\d{4}-\d\d-\d\d): (?:\d+ zaps\S{1,4}no|pr\S{1,4}chod)")
#: Okno začátku UTC dne; do #1674 první archiv běžel na pozadí, ES zapsal do ~00:02
WINDOW = dt.time(0, 15)


def scan(paths: list[Path]) -> tuple[list[tuple[dt.datetime, dt.datetime]], dict[str, set[str]]]:
    ranges: list[tuple[dt.datetime, dt.datetime]] = []
    reads: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        first: dt.datetime | None = None
        last: dt.datetime | None = None
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = TS.match(line)
                if not match:
                    continue
                ts = dt.datetime.fromisoformat(f"{match.group(1)} {match.group(2)}")
                first = first or ts
                last = ts
                if ts.time() < WINDOW:
                    read = READ.search(line)
                    if read and read.group(2) == match.group(1):
                        reads[match.group(1)].add(read.group(1))
        if first is not None and last is not None:
            ranges.append((first, last))
    return ranges, reads


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("logs", nargs="+", type=Path)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # Windows konzole cp1250
    ranges, reads = scan(args.logs)
    covered: set[dt.date] = set()
    for first, last in ranges:
        day = first.date()
        while day <= last.date():
            start = dt.datetime.combine(day, dt.time(0))
            if first <= start and last >= dt.datetime.combine(day, WINDOW):
                covered.add(day)
            day += dt.timedelta(days=1)
    print("| UTC den | den v týdnu | ES četl od 00:00 | NQ četl od 00:00 |")
    print("|---|---|---|---|")
    totals = {"ES": 0, "NQ": 0}
    for day in sorted(covered):
        symbols = reads.get(day.isoformat(), set())
        for symbol in totals:
            totals[symbol] += symbol in symbols
        marks = ["ano" if symbol in symbols else "**ne**" for symbol in ("ES", "NQ")]
        print(f"| {day} | {day:%a} | {marks[0]} | {marks[1]} |")
    print(f"\nDnů s logem přes celé okno 00:00–00:15 UTC: {len(covered)}")
    print(f"četl ES {totals['ES']}, NQ {totals['NQ']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
