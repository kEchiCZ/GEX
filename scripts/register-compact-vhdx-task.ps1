# Registrace elevované úlohy „GEXLens compact-vhdx" do Task Scheduleru (#1127).
#
# Kompaktace VHDX Docker Desktopu vyžaduje správce (diskpart). Úloha běží
# s nejvyššími právy pod tvým účtem, takže ji může spustit i běžný proces
# (docker-cleanup.ps1 po úklidu image, deploy ve 23:00) bez UAC dialogu.
# Vlastní trigger: sobota 10:00 (trh celý den zavřený). Skript sám hlídá
# prahy (-IfNeeded: VHDX > 40 GB a ≥ 10 GB k vrácení) i zavřený trh.
#
# SPUSTIT JEDNOU JAKO SPRÁVCE:  pwsh -File scripts/register-compact-vhdx-task.ps1
# Odregistrace: Unregister-ScheduledTask -TaskName 'GEXLens compact-vhdx' -Confirm:$false
# Ruční spuštění (bez UAC): Start-ScheduledTask -TaskName 'GEXLens compact-vhdx'
[CmdletBinding()]
param(
    [string]$TaskName = 'GEXLens compact-vhdx',
    [string]$At = '10:00'
)
$ErrorActionPreference = 'Stop'
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Registrace elevované úlohy vyžaduje správce — spusť pwsh jako správce.'
}
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo 'scripts\compact-docker-vhdx.ps1'
$log = Join-Path $repo 'data\logs\compact-vhdx.log'
if (-not (Test-Path $script)) { throw "Nenalezen $script" }
New-Item -ItemType Directory -Force (Split-Path -Parent $log) | Out-Null

# Plná cesta pwsh — Task Scheduler nepoužívá PATH uživatele (10. 9. 2026: 0x80070002)
$pwsh = (Get-Command pwsh).Source
$argument = "-NoProfile -ExecutionPolicy Bypass -Command `"& '$script' -IfNeeded *>> '$log'`""
$action = New-ScheduledTaskAction -Execute $pwsh -Argument $argument -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
# Interactive: Docker Desktop (GUI) se po kompaktaci startuje v přihlášené relaci
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'GEXLens: kompaktace VHDX Docker Desktopu, jen nad prahem a při zavřeném trhu (#1127); nic nemaže' -Force | Out-Null
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Registrováno: '$TaskName' — $($task.State); sobota $At + na vyžádání z docker-cleanup.ps1; log $log"
Write-Host "Zkouška nanečisto (jen rozhodnutí): pwsh -File `"$script`" -IfNeeded -DryRun"
