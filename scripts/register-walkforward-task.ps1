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
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo 'scripts\walkforward-nightly.ps1'
$log = Join-Path $repo 'data\reports\walkforward-nightly.log'
if (-not (Test-Path $script)) { throw "Nenalezen $script" }
New-Item -ItemType Directory -Force (Split-Path -Parent $log) | Out-Null

$pwsh = (Get-Command pwsh).Source
$argument = "-NoProfile -ExecutionPolicy Bypass -Command `"& '$script' *>> '$log'`""
$action = New-ScheduledTaskAction -Execute $pwsh -Argument $argument -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'GEXLens: noční walk-forward parametrů setupů (#794, ADR-0034) — jen report, nic nezapisuje' -Force | Out-Null
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Registrováno: '$TaskName' — $($task.State); spouští $At po–pá, log $log"
