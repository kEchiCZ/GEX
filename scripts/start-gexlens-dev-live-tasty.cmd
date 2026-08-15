@echo off
rem GEXLens DEV + ENGINE tasty-only (#623) — laborator providera bez IBKR.
rem PRODUKCE BEZI DAL (tasty snese soubezne streamy, ADR-0027).
title GEXLens DEV+Tasty
cd /d "%~dp0.."
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\start-dev.ps1 -LiveTasty
if errorlevel 1 pause
