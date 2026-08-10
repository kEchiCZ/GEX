@echo off
rem GEXLens PROD — cil zastupce na plose (#568: deleguje na start-prod.ps1,
rem ktery hlida exkluzivitu s dev-live a u buildu vetev main).
title GEXLens PROD
cd /d "%~dp0.."
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\start-prod.ps1
if errorlevel 1 pause
