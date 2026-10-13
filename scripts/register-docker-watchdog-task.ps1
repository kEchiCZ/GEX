# Registrace úlohy „GEXLens docker-watchdog" do Task Scheduleru (#1552, varianta B).
#
# Každých 5 min spustí docker-watchdog.ps1: když daemon neodpovídá a Docker
# Desktop není running, restartuje ho a ověří kontejnery a spojení enginu.
# Běží pod tvým účtem v přihlášené relaci (Docker Desktop je GUI aplikace),
# bez elevace a bez okna konzole (conhost --headless).
#
# SPUSTIT JEDNOU:  pwsh -File scripts/register-docker-watchdog-task.ps1
# Pozastavení (záměrné zastavení Dockeru): Disable-ScheduledTask -TaskName 'GEXLens docker-watchdog'
# Odregistrace: Unregister-ScheduledTask -TaskName 'GEXLens docker-watchdog' -Confirm:$false
[CmdletBinding()]
param(
    [string]$TaskName = 'GEXLens docker-watchdog',
    [int]$EveryMinutes = 5
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\DockerHost.ps1')
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo 'scripts\docker-watchdog.ps1'
if (-not (Test-Path $script)) { throw "Nenalezen $script" }
# Log mimo repo: D:\Documents chrání Řízený přístup ke složkám (lessons-learned 2026-09-10).
# Hlídač zapisuje jen při výpadku, takže log neroste.
$log = Join-Path (Get-OpsLogDir) 'docker-watchdog.log'

# Plná cesta pwsh přes App Execution Alias — přežije aktualizaci verze (lessons-learned 2026-09-24)
$pwsh = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'
if (-not (Test-Path $pwsh)) { $pwsh = (Get-Command pwsh).Source }
# conhost --headless: běh každých 5 min by jinak bliknul oknem konzole
$argument = "--headless `"$pwsh`" -NoProfile -ExecutionPolicy Bypass -Command `"& '$script' *>> '$log'`""
$action = New-ScheduledTaskAction -Execute (Join-Path $env:windir 'System32\conhost.exe') -Argument $argument -WorkingDirectory $repo
# Opakování bez konce od dnešní půlnoci; interval je relativní, posun DST nevadí
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
# 20 min: restart (≤ 5,5) + daemon (≤ 5) + engine (≤ 5) + rezerva
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -MultipleInstances IgnoreNew -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'GEXLens: hlídač Dockeru — restart Docker Desktopu, když daemon neodpovídá a Docker Desktop neběží (#1552)' -Force | Out-Null
$task = Get-ScheduledTask -TaskName $TaskName
$duration = $task.Triggers[0].Repetition.Duration
Write-Host "Registrováno: '$TaskName' — $($task.State); každých $EveryMinutes min (trvání opakování: $(if ($duration) { $duration } else { 'bez konce' })); log $log"
Write-Host "Zkouška nanečisto: pwsh -File `"$script`" -DryRun"
