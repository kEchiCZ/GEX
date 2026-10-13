# Registrace elevované úlohy „GEXLens update-offhours" do Task Scheduleru (#1552).
#
# Denně 23:10 místního času (pauza Globexu 23:00–24:00 SELČ i SEČ; v týdnech
# posunu letního času USA vs. EU vyjde do otevřeného trhu a brána skriptu běh
# přeskočí — dožene ho sobota). Skript update-wsl-docker-offhours.ps1 sám hlídá
# zavřený trh, čeká na deploy a kompaktaci a Docker Desktop aktualizuje jen po
# úspěšné záloze PG. Běžná noc bez nové verze trvá do minuty; noc s aktualizací
# Docker Desktopu ~15 min a může se potkat s walk-forward (23:30, jen report).
# Elevace: instalace MSI WSL a Docker Desktopu ji vyžaduje.
#
# SPUSTIT JEDNOU JAKO SPRÁVCE:  pwsh -File scripts/register-update-offhours-task.ps1
# Odregistrace: Unregister-ScheduledTask -TaskName 'GEXLens update-offhours' -Confirm:$false
# Ruční spuštění (bez UAC): Start-ScheduledTask -TaskName 'GEXLens update-offhours'
[CmdletBinding()]
param(
    [string]$TaskName = 'GEXLens update-offhours',
    [string]$At = '23:10'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\LocalTrigger.ps1')
. (Join-Path $PSScriptRoot 'lib\DockerHost.ps1')
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Registrace elevované úlohy vyžaduje správce — spusť pwsh jako správce.'
}
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo 'scripts\update-wsl-docker-offhours.ps1'
if (-not (Test-Path $script)) { throw "Nenalezen $script" }
# Log mimo repo: D:\Documents chrání Řízený přístup ke složkám (lessons-learned 2026-09-10)
$log = Join-Path (Get-OpsLogDir) 'update-offhours.log'

# Plná cesta pwsh přes App Execution Alias — přežije aktualizaci verze (lessons-learned 2026-09-24)
$pwsh = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'
if (-not (Test-Path $pwsh)) { $pwsh = (Get-Command pwsh).Source }
# conhost --headless: bez okna konzole na ploše
$argument = "--headless `"$pwsh`" -NoProfile -ExecutionPolicy Bypass -Command `"& '$script' *>> '$log'`""
$action = New-ScheduledTaskAction -Execute (Join-Path $env:windir 'System32\conhost.exe') -Argument $argument -WorkingDirectory $repo
# Místní čas, ne UTC (#1277/#1278)
$trigger = New-LocalWeeklyTrigger -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday -At $At
# 120 min: čekání na deploy a kompaktaci (≤ 20) + WSL s kontrolou stacku (≤ 15 + 25) + Docker Desktop se zálohou a kontrolou (≤ 55)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 120) `
    -MultipleInstances IgnoreNew -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
# Interactive: Docker Desktop (GUI) se po aktualizaci startuje v přihlášené relaci
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'GEXLens: aktualizace WSL a Docker Desktopu jen v pauze Globexu, Docker Desktop jen po záloze PG (#1552)' -Force | Out-Null
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Registrováno: '$TaskName' — $($task.State); denně $At místního času; log $log"
Write-Host "Zkouška nanečisto: pwsh -File `"$script`" -DryRun"
