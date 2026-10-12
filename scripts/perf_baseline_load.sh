#!/usr/bin/env bash
# Souběžná zátěž k měření výkonu grafu (#1412, E-2a.2; přeměření E-2a.18).
#
# Jeden snímek: čas UTC, `docker stats` kontejnerů a senzory CPU/GPU/RAM
# z LibreHardwareMonitoru (webový server :8085, Options → Remote Web Server).
# Spouští se před každým blokem scénářů a po něm, výstup jde do protokolu
# `docs/research/1381-vykon-baseline.md`.
#
#   bash scripts/perf_baseline_load.sh [štítek]
set -euo pipefail

LHM_URL="${LHM_URL:-http://localhost:8085/data.json}"
label="${1:-}"

echo "### zátěž $(date -u +%Y-%m-%dT%H:%M:%SZ) ${label}"
docker stats --no-stream --format '{{.Name}}\tCPU {{.CPUPerc}}\tRAM {{.MemUsage}}'

if ! sensors="$(curl -sf -m 5 "$LHM_URL")"; then
  echo "LHM: nedostupný na $LHM_URL — teplota a takty chybí" >&2
  exit 1
fi
printf '%s' "$sensors" | python -c '
import json, sys

# Windows: konzole i stdin v cp1250 by rozbily „°C“ a diakritiku
sys.stdout.reconfigure(encoding="utf-8")
WANTED = {
    ("Temperatures", "CPU Package"): "CPU package",
    ("Temperatures", "Core Max"): "jádro max",
    ("Powers", "CPU Package"): "příkon CPU",
    ("Load", "CPU Total"): "zátěž CPU",
    ("Load", "Memory"): "RAM",
    ("Load", "GPU Core"): "zátěž GPU",
}
values, clocks = {}, []

def walk(node, path):
    kids = node.get("Children", [])
    if not kids and node.get("Value"):
        group = path[-1] if path else ""
        text = node.get("Text", "")
        if group == "Clocks" and text.startswith("CPU Core #"):
            clocks.append(float(node["Value"].split()[0].replace(",", ".")))
        key = WANTED.get((group, text))
        # RAM: první „Memory“ pod Total Memory, ne virtuální paměť
        if key and key not in values and not (key == "RAM" and "Virtual" in " ".join(path)):
            values[key] = node["Value"]
    for kid in kids:
        walk(kid, path + [node.get("Text", "")])

walk(json.loads(sys.stdin.buffer.read().decode("utf-8")), [])
for key, value in values.items():
    print(f"{key}: {value}")
if clocks:
    print(f"takt jader: {min(clocks):.0f}–{max(clocks):.0f} MHz")
'
