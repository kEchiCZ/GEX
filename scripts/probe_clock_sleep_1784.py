"""Sonda #1784: započítají hodiny VM WSL2 dobu, kdy notebook spí?

`stale_age` kotace je `time.monotonic()` minus okamžik příjmu (`ibkr/scheduler.py`,
`CachedQuote.age_s`). `CLOCK_MONOTONIC` podle `clock_gettime(2)` nepočítá dobu uspání.
Když stojí i přes pauzu VM při spánku hostitele, vypadá kotace z doby před spánkem po
probuzení čerstvě (ES 6. 11. 15:11 UTC: kotace z 08:55 se stářím 2,7–14 s).

Sonda běží zároveň ve VM (kontejner) a na hostiteli Windows a každých `--interval` s
vypíše řádek JSON s `time.time()`, `time.monotonic()` a `CLOCK_BOOTTIME` (jen Linux).
Rozbor najde spánek jako největší mezeru v nástěnném čase hostitele a pro každé hodiny VM
spočítá, kolik z něj započetly. Nic nečte z dat ani z IBKR, nic nezapisuje mimo stdout.

Postup (Git Bash, kořen repa, v pauze Globexu nebo o víkendu; uspání dělá vlastník):
    MSYS_NO_PATHCONV=1 docker run -d --name gex-probe-clock --network none \\
        -v "$(pwd -W)/scripts:/s:ro" --entrypoint python \\
        "$(docker inspect --format '{{.Image}}' gex-engine-1)" \\
        -u /s/probe_clock_sleep_1784.py record vm
    .venv/Scripts/python.exe -u scripts/probe_clock_sleep_1784.py record host > host.jsonl &
    # ~1 min záznamu, uspat notebook na ~5 min, probudit, ~1 min záznamu
    docker logs gex-probe-clock > vm.jsonl; docker rm -f gex-probe-clock; kill %1
    .venv/Scripts/python.exe scripts/probe_clock_sleep_1784.py analyze host.jsonl vm.jsonl
"""

import argparse
import json
import sys
import time
from pathlib import Path

CLOCKS = ("wall", "mono", "boot")


def record(source: str, interval: float) -> None:
    """Vypisuje řádky JSON, dokud ji někdo neukončí."""
    boottime = getattr(time, "CLOCK_BOOTTIME", None)
    seq = 0
    while True:
        line = {
            "src": source,
            "seq": seq,
            "wall": time.time(),
            "mono": time.monotonic(),
            "boot": time.clock_gettime(boottime) if boottime is not None else None,
        }
        print(json.dumps(line), flush=True)
        seq += 1
        time.sleep(interval)


def load(path: Path) -> list[dict[str, float | None]]:
    rows = [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines() if text]
    if len(rows) < 2:
        raise SystemExit(f"{path}: méně než 2 řádky — sonda neběžela")
    return rows


def largest_step(rows: list[dict[str, float | None]], clock: str) -> tuple[float, int]:
    """Největší přírůstek hodin mezi sousedními řádky a seq řádku za ním."""
    best, at = 0.0, -1
    for prev, cur in zip(rows, rows[1:], strict=False):
        a, b = prev[clock], cur[clock]
        if a is None or b is None:
            continue
        if b - a > best:
            best, at = b - a, int(cur["seq"] or 0)
    return best, at


def analyze(host_path: Path, vm_path: Path) -> None:
    host, vm = load(host_path), load(vm_path)
    sleep, sleep_seq = largest_step(host, "wall")
    host_span = float(host[-1]["wall"] or 0) - float(host[0]["wall"] or 0)
    print(f"hostitel: {len(host)} řádků, rozpětí {host_span:.1f} s")
    print(f"hostitel: největší krok nástěnného času {sleep:.1f} s (před seq {sleep_seq}) = spánek")
    for clock in CLOCKS:
        if vm[0][clock] is None:
            print(f"VM {clock}: nedostupné")
            continue
        span = float(vm[-1][clock] or 0) - float(vm[0][clock] or 0)
        step, seq = largest_step(vm, clock)
        print(f"VM {clock}: rozpětí {span:.1f} s, největší krok {step:.1f} s (před seq {seq})")
    if sleep < 60:
        raise SystemExit("Spánek nenalezen: největší krok hostitele je pod 60 s")
    mono_step, _ = largest_step(vm, "mono")
    verdict = "ZAPOČÍTÁ" if mono_step >= 0.8 * sleep else "NEZAPOČÍTÁ"
    print(f"Verdikt: time.monotonic() ve VM spánek {verdict} ({mono_step:.1f} s z {sleep:.1f} s)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="vypisovat hodiny jako JSON řádky")
    rec.add_argument("source", choices=("vm", "host"))
    rec.add_argument("--interval", type=float, default=5.0)
    ana = sub.add_parser("analyze", help="porovnat záznam hostitele a VM")
    ana.add_argument("host", type=Path)
    ana.add_argument("vm", type=Path)
    args = parser.parse_args()
    if args.cmd == "record":
        try:
            record(args.source, args.interval)
        except KeyboardInterrupt:
            sys.exit(0)
    else:
        analyze(args.host, args.vm)


if __name__ == "__main__":
    main()
