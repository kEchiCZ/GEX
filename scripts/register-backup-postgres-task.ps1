# Registrace noční zálohy PostgreSQL do Task Scheduleru (#1566).
#
# Úloha „GEXLens backup-postgres": denně 23:50 místního času, v pauze Globexu
# (23:00–24:00 SELČ i SEČ) za kompaktací (po, st, pá 23:05), update-offhours
# (23:10) a walk-forwardem (po–pá 23:30, ~15 min). V týdnech posunu letního času
# USA vs. EU vyjde do otevřeného trhu — pg_dump sběr nezastaví, jen běží vedle něj.
# Zmeškaný start se dožene při nejbližší příležitosti (StartWhenAvailable).
# Skript backup-postgres-nightly.ps1 sám čeká na zámek údržby a hlásí selhání.
#
# Spustit jednou pod účtem, který má docker (bez správce):
#   pwsh -File scripts/register-backup-postgres-task.ps1
# Odregistrace: Unregister-ScheduledTask -TaskName 'GEXLens backup-postgres' -Confirm:$false
# Ruční spuštění: Start-ScheduledTask -TaskName 'GEXLens backup-postgres'
[CmdletBinding()]
param(
    [string]$TaskName = 'GEXLens backup-postgres',
    [string]$At = '23:50'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\LocalTrigger.ps1')
. (Join-Path $PSScriptRoot 'lib\DockerHost.ps1')
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo 'scripts\backup-postgres-nightly.ps1'
if (-not (Test-Path $script)) { throw "Nenalezen $script" }
# Log mimo repo: D:\Documents chrání Řízený přístup ke složkám (lessons-learned 2026-09-10, #1638)
$log = Join-Path (Get-OpsLogDir) 'backup-postgres.log'

# Plná cesta pwsh přes App Execution Alias — přežije aktualizaci verze (lessons-learned 2026-09-24)
$pwsh = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'
if (-not (Test-Path $pwsh)) { $pwsh = (Get-Command pwsh).Source }
# conhost --headless: bez okna konzole na ploše
$argument = "--headless `"$pwsh`" -NoProfile -ExecutionPolicy Bypass -Command `"& '$script' *>> '$log'`""
$action = New-ScheduledTaskAction -Execute (Join-Path $env:windir 'System32\conhost.exe') -Argument $argument -WorkingDirectory $repo
# Místní čas, ne UTC (#1277/#1278)
$trigger = New-LocalWeeklyTrigger -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday -At $At
# 60 min: čekání na kompaktaci nebo aktualizaci (≤ 30) + dump s kontrolou (limit 20, obvykle ~2)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 60) `
    -MultipleInstances IgnoreNew -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'GEXLens: noční záloha PostgreSQL s kontrolou celým čtením pg_restore a rotací 14 dumpů (#1566)' -Force | Out-Null
$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Registrováno: '$TaskName' — $($task.State); denně $At místního času; log $log"
Write-Host "Zkouška: Start-ScheduledTask -TaskName '$TaskName'; pak Get-Content '$log' -Tail 5"
