#!/usr/bin/env bash
# Obnova GEXLens ze zálohy jedním příkazem na Linuxovém serveru, #1800.
#
# Zastaví zapisující služby (engine, news-engine, api; postgres a IB Gateway běží
# dál), spustí `python -m gexlens_api.restore` v kontejneru api přes
# `docker compose run --rm` se zdrojem připojeným jen pro čtení, po úspěchu
# nastartuje stack (`up -d`) a ověří /status. Ověření zdroje, pojistka cíle
# i samotná obnova jsou v api/src/gexlens_api/restore.py (ADMIN-MANUAL kap. 14
# „Obnova ze zálohy“). Protějšek pro PC je scripts/restore.ps1.
#
# Výsledek obnovy: 0 = hotovo; 1 = cíl beze změny (služby se vrátí do stavu
# před obnovou); 2 = PG obnovená, data/ jen zčásti (služby zůstanou zastavené,
# obnovu spusť znovu s --overwrite).
#
# Použití (z kořene repozitáře, uživatel ve skupině docker):
#   scripts/restore.sh /srv/obnova/gexlens-zaloha-2026-11-25_2210-cd7f1d9.tar
#   scripts/restore.sh /srv/obnova/snimek --overwrite     # cíl s daty
# Compose soubory bere z COMPOSE_FILE, výchozí je server (compose.yml:compose.server.yml).
set -euo pipefail

usage() {
  echo "Použití: $0 <balíček.tar | adresář> [--overwrite]" >&2
  exit 64
}

[[ $# -ge 1 ]] || usage
if ! source_path=$(realpath -e -- "$1"); then
  echo "Zdroj $1 neexistuje" >&2
  exit 1
fi
shift
options=()
for arg in "$@"; do
  case $arg in
    --overwrite) options+=(--overwrite) ;;
    *) usage ;;
  esac
done

cd "$(dirname "$0")/.."
export COMPOSE_FILE="${COMPOSE_FILE:-compose.yml:compose.server.yml}"

# Jen zapisující služby, které právě běží: při selhání se vrátí přesně ty
services=$(docker compose ps --services --status running)
running=()
for service in engine news-engine api; do
  if grep -Fxq -- "$service" <<<"$services"; then
    running+=("$service")
  fi
done

docker compose up -d --wait postgres
if [[ -d $source_path ]]; then
  inside=/restore/zdroj
else
  inside="/restore/$(basename -- "$source_path")"
fi

if ((${#running[@]})); then
  echo "Zastavuji ${running[*]}…"
  docker compose stop "${running[@]}"
fi
code=0
docker compose run --rm --no-deps -T -v "$source_path:$inside:ro" api \
  python -m gexlens_api.restore "$inside" "${options[@]}" || code=$?

if ((code == 2)); then
  echo "Obnova nedokončena: PG je obnovená, data/ jen zčásti. Služby zůstávají zastavené —" \
    "spusť obnovu znovu s --overwrite." >&2
  exit 2
fi
if ((code != 0)); then
  if ((${#running[@]})); then
    echo "Cíl zůstal beze změny, startuji ${running[*]}…" >&2
    docker compose start "${running[@]}"
  fi
  echo "Obnova selhala (kód $code) — důvod je ve výpisu výše." >&2
  exit "$code"
fi

echo "Startuji stack…"
docker compose up -d
address=$(docker compose port api 8000 | head -n 1)
for _ in $(seq 1 30); do
  if body=$(curl -fsS --max-time 5 "http://$address/status"); then
    echo "/status odpovídá: ${body:0:200}"
    exit 0
  fi
  sleep 3
done
echo "/status na $address neodpověděl do 90 s" >&2
exit 1
