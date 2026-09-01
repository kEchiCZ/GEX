#!/usr/bin/env bash
# Kontrola #939: konverguje cílený heal (#936/#937) za RTH?
# Spustit ~45 min po startu US RTH (13:30 UTC), tedy od 14:15 UTC.
# Základ PŘED fixem (28. 8. RTH): 79 healů/2 h, rate_limited 5 881/den,
# stream_errors 21 548/den.
okno="${1:-2h}"
echo "=== #939 — heal za RTH, okno $okno ($(date -u)) ==="
echo
echo "-- /status (kumulativ od startu enginu) --"
curl -s --max-time 10 http://127.0.0.1:8000/status | python -c "
import sys, json
d = json.load(sys.stdin)
for k in ('tasty_heals','tasty_rate_limited','tasty_stream_errors','tasty_reconnects','tasty_symbols','tasty_connected','feed_crosscheck','spot_source','chain_source'):
    print(f'  {k:22} {d.get(k)}')
"
echo
echo "-- healy v logu za $okno (N/M — N má být malé) --"
docker logs gex-engine-1 --since "$okno" 2>&1 | grep -oE "heal [0-9]+/[0-9]+ symbolů" | tail -8
echo "  celkem healů: $(docker logs gex-engine-1 --since "$okno" 2>&1 | grep -c 'heal [0-9]*/[0-9]* symbolů')"
echo
echo "-- tok dat drží? (poslední cykly) --"
docker logs gex-engine-1 --since 10m 2>&1 | grep -E "Cyklus (ES|NQ)" | tail -4
echo
echo "-- uptime enginu (kumulativy jsou od něj) --"
docker ps --format '{{.Names}} {{.Status}}' | grep gex-engine
