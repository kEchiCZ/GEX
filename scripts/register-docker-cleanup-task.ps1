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
. (Join-Path $PSScriptRoot 'lib\LocalTrigger.ps1')
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo 'scripts\docker-cleanup.ps1'
$log = Join-Path $repo 'data\logs\docker-cleanup.log'
if (-not (Test-Path $script)) { throw "Nenalezen $script" }
New-Item -ItemType Directory -Force (Split-Path -Parent $log) | Out-Null

# Plná cesta k pwsh: Task Scheduler pod Řízeným přístupem ke složkám (Defender)
# spouští jen známé binárky s plnou cestou (ADMIN manuál kap. 12)
# Stabilní alias pwsh (App Execution Alias), ne cesta do WindowsApps s číslem
# verze: po aktualizaci Store balíčku (7.6.5 → 7.6.6, 9. 9. 2026) stará cesta
# zmizela a úlohy končily 0x80070002, aniž si toho kdo všiml (walk-forward
# neběžel 2 týdny). Alias verzi přežije; Get-Command jen jako záloha.
$pwsh = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'
if (-not (Test-Path $pwsh)) { $pwsh = (Get-Command pwsh).Source }
$argument = "-NoProfile -ExecutionPolicy Bypass -Command `"& '$script' *>> '$log'`""
$action = New-ScheduledTaskAction -Execute $pwsh -Argument $argument -WorkingDirectory $repo
$trigger = New-LocalWeeklyTrigger -DaysOfWeek Saturday -At $At  # místní čas (#1278)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'GEXLens: týdenní úklid Dockeru (#1127) — rollback tagy, build cache, dangling image, hlídka místa; volumes se nedotýká' -Force | Out-Null
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Registrováno: '$TaskName' — $($task.State); spouští sobota $At, log $log"
