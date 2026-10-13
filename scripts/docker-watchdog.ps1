# Hlídač Dockeru na hostiteli (#1552, varianta B; rozhodnutí vlastníka 13. 10. 2026).
#
# 13. 10. 2026 Windows Update sám nainstaloval WSL 3.0.3.0, ukončil VM Dockeru
# a Docker Desktop engine sám neobnovil: produkce 4 h 49 min dole během Globexu
# a nikdo o tom nevěděl — hlídače žijí uvnitř kontejnerů, které neběžely.
# Tento skript běží MIMO Docker každých 5 min (úloha „GEXLens docker-watchdog",
# register-docker-watchdog-task.ps1):
#   - `docker info` odpovídá → nic (konec incidentu se zapíše do logu);
#   - neodpovídá a `docker desktop status` ≠ running → `docker desktop restart`,
#     čekání na daemon, výpis kontejnerů gex-*, spojení enginu s IBKR (/status);
#   - running, ale daemon neodpovídá → jen upozornění (restart jen při ≠ running);
#   - běží deploy (příkazový řádek) nebo kompaktace / aktualizace v pauze (zámek
#     údržby — elevované procesy neelevovaný hlídač jinak nevidí) → bez zásahu,
#     Docker restartují záměrně a samy ho hlídají.
# Restart proběhne kdykoli (díra v datech minuty místo hodin); upozornění na
# Telegram (přepínač „maintenance") jen při otevřeném trhu podle
# compute/marketclock.is_market_closed (Test-MarketClosed, zná svátky CME) —
# při zavřeném trhu jen log (AGENTS.md: zavřený trh = žádná upozornění).
# Jedno upozornění na incident; stav incidentu v %LOCALAPPDATA%\GEXLens.
#
# Záměrné zastavení Docker Desktopu: nejdřív
#   Disable-ScheduledTask -TaskName 'GEXLens docker-watchdog'
# jinak ho hlídač do 5 min znovu spustí.
#
#   pwsh -File scripts/docker-watchdog.ps1           jeden průchod (to dělá úloha)
#   pwsh -File scripts/docker-watchdog.ps1 -DryRun   jen rozhodnutí, nic nerestartuje
[CmdletBinding()]
param(
    [switch]$DryRun,
    # Jak dlouho čekat na daemon a na spojení enginu po restartu
    [int]$ReadyMinutes = 5,
    [int]$EngineMinutes = 5
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\GlobexClock.ps1')
. (Join-Path $PSScriptRoot 'lib\OpsAlert.ps1')
. (Join-Path $PSScriptRoot 'lib\DockerHost.ps1')

$statePath = Join-Path (Split-Path -Parent (Get-OpsLogDir)) 'docker-watchdog.json'
function Read-Incident {
    if (-not (Test-Path $statePath)) { return $null }
    try { Get-Content $statePath -Raw | ConvertFrom-Json } catch { $null }
}
function Save-Incident($incident) { $incident | ConvertTo-Json | Set-Content -Path $statePath -Encoding utf8 }

$infoOk = Test-DockerReady
$incident = Read-Incident
if ($infoOk) {
    if ($incident) {
        Write-Step "Docker zase odpovídá (incident od $($incident.since))."
        if (-not $DryRun) {
            # Kdo dostal upozornění na výpadek, dostane i konec (jen při otevřeném trhu)
            if ($incident.alerted -and -not (Test-MarketClosed)) {
                Send-OpsAlert "Docker zase odpovídá (výpadek od $($incident.since))." -Topic 'maintenance'
            }
            Remove-Item $statePath -Force
        }
    } elseif ($DryRun) { Write-Step 'DryRun: Docker odpovídá — nic.' }
    exit 0
}

# Úlohy, které Docker zastavují záměrně a samy ho po sobě obnovují: deploy
# (neelevovaný, příkazový řádek) a kompaktace / aktualizace (zámek údržby)
$busy = @(Get-BusyJobs 'deploy-engine-offhours') + @(Get-MaintenanceLockHolder | Where-Object { $_ })
$status = Get-DockerDesktopStatus
$now = Get-Date
if (-not $incident) { $incident = [pscustomobject]@{ since = $now.ToString('s'); alerted = $false } }
$minutes = ($now - [datetime]$incident.since).TotalMinutes
$action = Resolve-WatchdogAction -InfoOk $false -Status $status -Busy $busy -IncidentMinutes $minutes
$clock = Get-GlobexClock
$closed = Test-MarketClosed
$market = if ($closed) { 'trh zavřený' } else { 'trh otevřený' }
Write-Step "Docker neodpovídá: stav Docker Desktopu '$status', incident od $($incident.since) ($([math]::Round($minutes)) min), $market (CT $($clock.Label)) → $action"
if ($DryRun) { exit 0 }
if ($action -eq 'skip-busy') { Write-Step "Běží $($busy -join ', ') — bez zásahu."; exit 0 }

# Nastavení Telegramu čte Send-OpsAlert z API; když API s Dockerem neodpovídá,
# posílá se (fail-open, OpsAlert). -Always: zpráva o konci incidentu (restart
# proběhl) jde i po dřívějším upozornění
function Send-IncidentAlert([string]$Message, [switch]$Always) {
    if ($closed) { Write-Step "Trh zavřený — bez upozornění: $Message"; return }
    if ($incident.alerted -and -not $Always) { Write-Step "Už upozorněno v tomto incidentu: $Message"; return }
    Send-OpsAlert $Message -Topic 'maintenance'
    $incident.alerted = $true
}

switch ($action) {
    'wait' { Save-Incident $incident; exit 0 }
    'hung' {
        Send-IncidentAlert "Docker daemon neodpovídá od $($incident.since), Docker Desktop přitom hlásí running — sběr dat může stát. Hlídač restartuje jen zastavený Docker; zkontroluj Docker Desktop."
        Save-Incident $incident
        exit 1
    }
}

# restart — incident uložit předem: restart a čekání trvají až ~23 min a úlohu by
# při překročení limitu zabil Task Scheduler bez zápisu
Save-Incident $incident
Write-Step 'docker desktop restart...'
$result = Invoke-Docker @('desktop', 'restart', '--timeout', '300') 330
if ($result.ExitCode -ne 0) {
    Write-Warning "docker desktop restart skončil $($result.ExitCode): $("$($result.Err)".Trim()) — zkouším docker desktop start."
    [void](Invoke-Docker @('desktop', 'start', '--timeout', '300') 330)
}
if (-not (Wait-DockerReady $ReadyMinutes)) {
    Send-IncidentAlert "Docker nenaběhl ani po restartu hlídačem (výpadek od $($incident.since)) — sběr dat stojí. Další pokus za 5 min; zkontroluj Docker Desktop."
    Save-Incident $incident
    exit 1
}

# Kontejnery s restart: unless-stopped naběhnou samy (kromě ručně zastavených)
Start-Sleep -Seconds 20
$services = @(Get-GexServices)
$running = @(Get-GexRunning)
$stopped = @($services | Where-Object { $_ -notin $running })
$engine = Wait-EngineConnected $EngineMinutes
$engineText = if ($engine) { "engine $($engine.Engine), IBKR $($engine.Connection)" } else { 'API /status neodpovídá' }
$summary = "kontejnery $($running.Count)/$($services.Count)" + $(if ($stopped.Count) { " (neběží $($stopped -join ', '))" } else { '' }) + ", $engineText"
Write-Step "Docker po restartu běží: $summary."
Send-IncidentAlert "Hlídač restartoval Docker Desktop (výpadek od $($incident.since), stav byl '$status'): $summary." -Always
Remove-Item $statePath -Force -ErrorAction SilentlyContinue
if ($stopped.Count -gt 0 -or -not $engine -or $engine.Connection -ne 'connected') { exit 1 }
