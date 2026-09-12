# Registrace týdenního úklidu Dockeru do Task Scheduleru (#1127).
#
# Úloha „GEXLens docker úklid": sobota 08:00 (trh zavřený od pátku 23:00 do
# neděle 23:00), jen když PC běží; zmeškaný start se dožene (StartWhenAvailable).
# Skript maže jen rollback tagy, starou build cache a osiřelé image; při
# nedostatku místa pošle alert do zvonku. Výstup jde do data/logs/docker-cleanup.log.
# Spustit jednou pod účtem, který má docker: pwsh scripts/register-docker-cleanup-task.ps1
# Odregistrace: Unregister-ScheduledTask -TaskName 'GEXLens docker úklid' -Confirm:$false
[CmdletBinding()]
param(
    [string]$TaskName = 'GEXLens docker úklid',
    [string]$At = '08:00'
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo 'scripts\docker-cleanup.ps1'
$log = Join-Path $repo 'data\logs\docker-cleanup.log'
if (-not (Test-Path $script)) { throw "Nenalezen $script" }
New-Item -ItemType Directory -Force (Split-Path -Parent $log) | Out-Null

# Plná cesta k pwsh: Task Scheduler pod Řízeným přístupem ke složkám (Defender)
# spouští jen známé binárky s plnou cestou (ADMIN manuál kap. 12)
$pwsh = (Get-Command pwsh).Source
$argument = "-NoProfile -ExecutionPolicy Bypass -Command `"& '$script' *>> '$log'`""
$action = New-ScheduledTaskAction -Execute $pwsh -Argument $argument -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'GEXLens: týdenní úklid Dockeru (#1127) — rollback tagy, build cache, dangling image, hlídka místa; volumes se nedotýká' -Force | Out-Null
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Registrováno: '$TaskName' — $($task.State); spouští sobota $At, log $log"
