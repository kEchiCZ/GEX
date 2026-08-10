@echo off
rem GEXLens DEV + ENGINE (#568) — plny stack proti TWS. NEJDRIV SHODI
rem PRODUKCI (jeden market data ucet); po dobu behu produkce nesbira.
title GEXLens DEV+Engine
cd /d "%~dp0.."
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\start-dev.ps1 -Live
if errorlevel 1 pause
