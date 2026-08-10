@echo off
rem GEXLens DEV bez enginu (#568) — bezi na :8081 nad kopii dat (data-dev,
rem gexdev_pgdata). Nedotkne se market data uctu, produkce muze bezet dal.
title GEXLens DEV
cd /d "%~dp0.."
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\start-dev.ps1
if errorlevel 1 pause
