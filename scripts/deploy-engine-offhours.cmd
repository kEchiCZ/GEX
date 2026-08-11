@echo off
rem Launcher pro naplánovanou úlohu (#600) — schtasks si s uvozovkami ve složeném
rem příkazu neporadí (rozbije je a úloha skončí na 0x80070002), takže se volá
rem tenhle .cmd a přesměrování řeší on.
rem
rem Naplánování na 23:15 (denní pauza Globexu, kdy se restartuje i TWS):
rem   schtasks /Create /TN "GEXLens-deploy-engine" /SC ONCE /ST 23:15 /F ^
rem     /TR "\"%~f0\""
rem UTF-8 konzole, jinak je diakritika v logu rozsypaná
chcp 65001 > nul
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy-engine-offhours.ps1" > "%USERPROFILE%\gexlens-deploy-engine.log" 2>&1
exit /b %ERRORLEVEL%
