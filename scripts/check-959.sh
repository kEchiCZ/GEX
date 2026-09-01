#!/usr/bin/env bash
# Kontrola #959: po settle NESMÍ přijít alert greeks_stalled pro expirující řadu.
# Spustit kdykoli po settle (v létě 20:00 UTC / 22:00 SELČ).
n=$(docker logs gex-engine-1 --since "${1:-4h}" 2>&1 | grep -c "nechodí pro")
echo "Alertů greeks_stalled za ${1:-4h}: $n"
if [ "$n" -eq 0 ]; then
  echo "VERDIKT: OK — #959 drží, falešný poplach po settle nepřišel."
else
  echo "VERDIKT: PROBLÉM — #959 nefunguje. Řádky:"
  docker logs gex-engine-1 --since "${1:-4h}" 2>&1 | grep "nechodí pro"
fi
echo
echo "Kontext (poslední cykly — expirující řada má po settle málo greeks, sekundární plno):"
docker logs gex-engine-1 --since 15m 2>&1 | grep -E "Cyklus (ES|NQ)" | tail -4
