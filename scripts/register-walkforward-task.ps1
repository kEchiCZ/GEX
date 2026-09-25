# Registrace noční úlohy walk-forward do Task Scheduleru (#794 fáze 3, ADR-0034).
#
# Úloha „GEXLens walk-forward": po–pá 23:30 (po settle 22:00), jen když PC běží;
# zmeškaný start se dožene při nejbližší příležitosti (StartWhenAvailable).
# Skript sám ověří, že běží aplikace (PG kontejner), jinak tiše skončí.
# Výstup se připisuje do data/reports/walkforward-nightly.log.
# Spustit jednou pod účtem, který má docker i uv: pwsh scripts/register-walkforward-task.ps1
# Odregistrace: Unregister-ScheduledTask -TaskName 'GEXLens walk-forward' -Confirm:$false
[CmdletBinding()]
param(
    [string]$TaskName = 'GEXLens walk-forward',
    [string]$At = '23:30'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\LocalTrigger.ps1')
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo 'scripts\walkforward-nightly.ps1'
$log = Join-Path $repo 'data\reports\walkforward-nightly.log'
if (-not (Test-Path $script)) { throw "Nenalezen $script" }
New-Item -ItemType Directory -Force (Split-Path -Parent $log) | Out-Null

# Stabilní alias pwsh (App Execution Alias), ne cesta do WindowsApps s číslem
# verze: po aktualizaci Store balíčku (7.6.5 → 7.6.6, 9. 9. 2026) stará cesta
# zmizela a úlohy končily 0x80070002, aniž si toho kdo všiml (walk-forward
# neběžel 2 týdny). Alias verzi přežije; Get-Command jen jako záloha.
$pwsh = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'
if (-not (Test-Path $pwsh)) { $pwsh = (Get-Command pwsh).Source }
$argument = "-NoProfile -ExecutionPolicy Bypass -Command `"& '$script' *>> '$log'`""
$action = New-ScheduledTaskAction -Execute $pwsh -Argument $argument -WorkingDirectory $repo
# Místní čas (#1278): s UTC ukotvením by po konci letního času běžel 22:30 v otevřeném trhu
$trigger = New-LocalWeeklyTrigger -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'GEXLens: noční walk-forward parametrů setupů (#794, ADR-0034) — jen report, nic nezapisuje' -Force | Out-Null
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Registrováno: '$TaskName' — $($task.State); spouští $At po–pá, log $log"
