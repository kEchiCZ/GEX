@echo off
rem Sonda #631 v pauze Globexu (23:00): stop engine -> sonda lines -> start engine.
rem Lines jsou sdilene per uzivatel - se spustenym enginem by sonda trhala produkci.
cd /d "D:\Documents\Visual Studio Code\GEX"
docker stop gex-engine-1 >> scripts\lines_probe_run.log 2>&1
timeout /t 10 /nobreak > nul
uv run python scripts\lines_probe.py >> scripts\lines_probe_run.log 2>&1
docker start gex-engine-1 >> scripts\lines_probe_run.log 2>&1
echo hotovo %date% %time% >> scripts\lines_probe_run.log
