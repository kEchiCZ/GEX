@echo off
rem GEXLens — spousteci skript (cil zastupce na plose).
rem Nastartuje docker compose stack a otevre aplikaci v prohlizeci.
title GEXLens start
cd /d "%~dp0.."

echo Startuji GEXLens...
docker compose up -d
if errorlevel 1 (
  echo.
  echo CHYBA: Docker nebezi. Spust Docker Desktop a zkus to znovu.
  pause
  exit /b 1
)

echo Cekam na aplikaci...
set /a tries=0
:wait
curl -s -o nul http://127.0.0.1:8080/ && goto ready
set /a tries+=1
if %tries% geq 60 (
  echo Aplikace nenabehla do 2 minut - zkontroluj "docker compose logs".
  pause
  exit /b 1
)
timeout /t 2 /nobreak >nul
goto wait

:ready
start "" http://127.0.0.1:8080/
echo GEXLens bezi: http://127.0.0.1:8080/
timeout /t 3 /nobreak >nul
