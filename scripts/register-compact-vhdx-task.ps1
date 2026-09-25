# Registrace elevované úlohy „GEXLens compact-vhdx" do Task Scheduleru (#1127).
#
# Kompaktace VHDX Docker Desktopu vyžaduje správce (diskpart). Úloha běží
# s nejvyššími právy pod tvým účtem, takže ji může spustit i běžný proces
# (docker-cleanup.ps1 po úklidu image na konci deploye) bez UAC dialogu.
# Vlastní triggery (#1277): po/st/pá 23:05 (pauza Globexu 16:00–17:00 CT,
# v pátek už víkend) a sobota 10:00 (trh celý den zavřený). Dřív jen sobota —
# odložený běh z docker-cleanup v otevřeném trhu pak čekal celý týden a D:
# kleslo na 4 GB. Skript sám hlídá prahy (-IfNeeded: VHDX > 40 GB a ≥ 10 GB
# k vrácení) i zavřený trh; v týdnech posunu letního času USA vs. EU vyjde
# 23:05 do otevřeného trhu a brána běh jen přeskočí.
#
# SPUSTIT JEDNOU JAKO SPRÁVCE:  pwsh -File scripts/register-compact-vhdx-task.ps1
# Odregistrace: Unregister-ScheduledTask -TaskName 'GEXLens compact-vhdx' -Confirm:$false
# Ruční spuštění (bez UAC): Start-ScheduledTask -TaskName 'GEXLens compact-vhdx'
[CmdletBinding()]
param(
    [string]$TaskName = 'GEXLens compact-vhdx',
    [string]$At = '10:00',
    # Pauza Globexu 23:00–24:00 CEST; kompaktace (~2–5 min) skončí před
    # walk-forward (23:30); na běžící deploy (engine 23:04–23:08) kompaktace počká
    [string]$WeekdayAt = '23:05'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\LocalTrigger.ps1')
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Registrace elevované úlohy vyžaduje správce — spusť pwsh jako správce.'
}
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo 'scripts\compact-docker-vhdx.ps1'
$log = Join-Path $repo 'data\logs\compact-vhdx.log'
if (-not (Test-Path $script)) { throw "Nenalezen $script" }
New-Item -ItemType Directory -Force (Split-Path -Parent $log) | Out-Null

# Plná cesta pwsh — Task Scheduler nepoužívá PATH uživatele (10. 9. 2026: 0x80070002)
# Stabilní alias pwsh (App Execution Alias), ne cesta do WindowsApps s číslem
# verze: po aktualizaci Store balíčku (7.6.5 → 7.6.6, 9. 9. 2026) stará cesta
# zmizela a úlohy končily 0x80070002, aniž si toho kdo všiml (walk-forward
# neběžel 2 týdny). Alias verzi přežije; Get-Command jen jako záloha.
$pwsh = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'
if (-not (Test-Path $pwsh)) { $pwsh = (Get-Command pwsh).Source }
$argument = "-NoProfile -ExecutionPolicy Bypass -Command `"& '$script' -IfNeeded *>> '$log'`""
$action = New-ScheduledTaskAction -Execute $pwsh -Argument $argument -WorkingDirectory $repo
# Místní čas, ne UTC (#1277/#1278): jinak by po konci letního času 23:05 vyšlo
# na 22:05, tedy do otevřeného trhu
$trigger = @(
    New-LocalWeeklyTrigger -DaysOfWeek Monday, Wednesday, Friday -At $WeekdayAt
    New-LocalWeeklyTrigger -DaysOfWeek Saturday -At $At
)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
# Interactive: Docker Desktop (GUI) se po kompaktaci startuje v přihlášené relaci
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'GEXLens: kompaktace VHDX Docker Desktopu, jen nad prahem a při zavřeném trhu (#1127); nic nemaže' -Force | Out-Null
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Registrováno: '$TaskName' — $($task.State); po/st/pá $WeekdayAt, so $At + na vyžádání z docker-cleanup.ps1; log $log"
Write-Host "Zkouška nanečisto (jen rozhodnutí): pwsh -File `"$script`" -IfNeeded -DryRun"
