# Docker a WSL na hostiteli — sdílené funkce provozních skriptů, které Docker
# zastavují, restartují nebo aktualizují (kompaktace VHDX #1127, aktualizace WSL
# a Docker Desktop v pauze a hlídač Dockeru #1552). Jedna definice místo kopií.
#
# Použití:  . (Join-Path $PSScriptRoot 'lib\DockerHost.ps1')

function Write-Step($text) { Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $text" }

# Logy úloh Task Scheduleru MIMO repo: repo leží v D:\Documents, které chrání
# Defender „Řízený přístup ke složkám" — pwsh tam zápis tiše nedostane
# („Could not find file", lessons-learned 2026-09-10). %LOCALAPPDATA% chráněné není.
function Get-OpsLogDir {
    $dir = Join-Path $env:LOCALAPPDATA 'GEXLens\logs'
    New-Item -ItemType Directory -Force $dir | Out-Null
    $dir
}

# Nativní příkaz s časovým limitem (#1279): při napůl živém daemonu `docker info`
# visí (lessons-learned: docker CLI při pádu lže/visí) a termín čekání by se
# nikdy nezkontroloval — úlohu by ve 60. minutě zabil Task Scheduler bez
# upozornění. Vypršení = příkaz nereaguje (ExitCode -1).
function Invoke-Native([string]$FilePath, [string[]]$ArgumentList, [int]$TimeoutSec = 20) {
    $psi = [System.Diagnostics.ProcessStartInfo]::new($FilePath)
    foreach ($arg in $ArgumentList) { $psi.ArgumentList.Add($arg) }
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    try { $proc = [System.Diagnostics.Process]::Start($psi) }
    catch { return [pscustomobject]@{ ExitCode = -1; Out = ''; Err = $_.Exception.Message } }
    $out = $proc.StandardOutput.ReadToEndAsync()
    $err = $proc.StandardError.ReadToEndAsync()
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        try { $proc.Kill($true) } catch { }
        return [pscustomobject]@{ ExitCode = -1; Out = ''; Err = "$FilePath $($ArgumentList[0]): bez odpovědi do $TimeoutSec s" }
    }
    [pscustomobject]@{ ExitCode = $proc.ExitCode; Out = $out.Result; Err = $err.Result }
}
function Invoke-Docker([string[]]$DockerArgs, [int]$TimeoutSec = 20) { Invoke-Native 'docker' $DockerArgs $TimeoutSec }

function Test-DockerReady { (Invoke-Docker @('info', '--format', '{{.ServerVersion}}') 20).ExitCode -eq 0 }

# Běžící služby produkčního projektu (bez one-off kontejnerů typu gex-greeks-probe
# a bez crash loopu — status=running); prázdné i při nereagujícím daemonu
function Get-GexRunning {
    $result = Invoke-Docker @('ps', '--filter', 'label=com.docker.compose.project=gex',
        '--filter', 'label=com.docker.compose.oneoff=False', '--filter', 'status=running', '--format', '{{.Names}}')
    if ($result.ExitCode -ne 0) { return }  # nic = daemon nereaguje; @($null) by mělo Count 1
    @($result.Out -split "`r?`n" | Where-Object { $_ } | Sort-Object)
}

# Všechny kontejnery služeb projektu včetně zastavených — po pádu daemonu nejde
# zjistit, co běželo předtím; `unless-stopped` je po startu daemonu spustí samy
# kromě ručně zastavených
function Get-GexServices {
    $result = Invoke-Docker @('ps', '-a', '--filter', 'label=com.docker.compose.project=gex',
        '--filter', 'label=com.docker.compose.oneoff=False', '--format', '{{.Names}}')
    if ($result.ExitCode -ne 0) { return }
    @($result.Out -split "`r?`n" | Where-Object { $_ } | Sort-Object)
}

# Po startu daemonu: kontejnery s restart: unless-stopped naběhnou samy; co
# z očekávaných chybí, spustí `docker start` (ne `compose up`: up by kontejner
# mohl recreatovat na novější image z GHCR, než ho nasadí deploy). Postgres
# první. Vrací kontejnery, které ani pak neběží (prázdné = vše běží).
function Restore-GexRunning([string[]]$Expected) {
    Start-Sleep -Seconds 20
    $missing = @($Expected | Where-Object { $_ -notin @(Get-GexRunning) })
    if ($missing.Count -eq 0) { return @() }
    $order = @($missing | Sort-Object { $_ -notlike '*postgres*' }, { $_ })
    Write-Warning "Neběží $($order -join ', ') — zkouším docker start."
    $result = Invoke-Docker (@('start') + $order) 120
    if ($result.ExitCode -ne 0) { Write-Warning "docker start skončil kódem $($result.ExitCode): $($result.Err.Trim())" }
    Start-Sleep -Seconds 20
    @($Expected | Where-Object { $_ -notin @(Get-GexRunning) })
}

function Wait-DockerReady([int]$Minutes) {
    $deadline = (Get-Date).AddMinutes($Minutes)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 10
        if (Test-DockerReady) { return $true }
    }
    return $false
}

# Souběžné provozní úlohy, které Docker zastavují nebo potřebují (#1277):
# deploy recreatuje engine, kompaktace a aktualizace Docker zastavují,
# walk-forward a záloha PG čtou Postgres. Vlastní proces se nepočítá.
function Get-BusyJobs([string]$Pattern) {
    @(Get-CimInstance Win32_Process -Filter "Name='pwsh.exe' OR Name='powershell.exe'" -ErrorAction SilentlyContinue |
        ForEach-Object { if ($_.ProcessId -ne $PID -and $_.CommandLine -match $Pattern) { $matches[0] } } |
        Sort-Object -Unique)
}

# Verze z registru odinstalace (MSI) — nezávisí na běžícím daemonu ani na jazyku
# výstupu `wsl --version` (UTF-16, lokalizovaný); $null = není nainstalováno
function Get-InstalledVersion([string]$DisplayName) {
    $keys = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
    $hit = Get-ItemProperty $keys -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -eq $DisplayName } |
        Select-Object -First 1
    if ($hit) { [string]$hit.DisplayVersion } else { $null }
}
function Get-WslVersion { Get-InstalledVersion 'Windows Subsystem for Linux' }
function Get-DockerDesktopVersion { Get-InstalledVersion 'Docker Desktop' }

# Stav Docker Desktopu: running / stopped / starting / …; 'unknown' = CLI neodpovědělo
function Get-DockerDesktopStatus {
    $result = Invoke-Docker @('desktop', 'status', '--format', 'json') 30
    if ($result.ExitCode -ne 0) { return 'unknown' }
    try { [string](($result.Out | ConvertFrom-Json).Status) } catch { 'unknown' }
}

# Stav enginu z API produkce (/status): online + spojení s IBKR; $null = API neodpovídá
function Get-EngineStatus([string]$ApiBase = 'http://127.0.0.1:8010') {
    try {
        $s = Invoke-RestMethod -Uri "$ApiBase/status" -TimeoutSec 10 -Verbose:$false -Debug:$false
        [pscustomobject]@{ Engine = [string]$s.engine; Connection = [string]$s.connection; LastTick = [string]$s.last_tick_ts }
    } catch { $null }
}
function Wait-EngineConnected([int]$Minutes, [string]$ApiBase = 'http://127.0.0.1:8010') {
    $deadline = (Get-Date).AddMinutes($Minutes)
    $last = $null
    while ((Get-Date) -lt $deadline) {
        $last = Get-EngineStatus $ApiBase
        if ($last -and $last.Engine -eq 'online' -and $last.Connection -eq 'connected') { return $last }
        Start-Sleep -Seconds 15
    }
    $last
}

# Přesné počty řádků všech tabulek schématu public, read-only (#1552): před a po
# aktualizaci Docker Desktopu — instalátor starších verzí uměl smazat datový disk
# WSL, na kterém leží volume pgdata. Vrací hashtable tabulka → počet; $null = chyba.
function Get-PgRowCounts([string]$Container = 'gex-postgres-1', [string]$Database = 'gexlens', [string]$User = 'gexlens') {
    $sql = "SELECT table_name, (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', " +
        "table_schema, table_name), false, true, '')))[1]::text FROM information_schema.tables " +
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1"
    $result = Invoke-Docker @('exec', '-e', 'PGOPTIONS=-c default_transaction_read_only=on', $Container,
        'psql', '-X', '-q', '-A', '-t', '-F', '|', '-v', 'ON_ERROR_STOP=1', '-U', $User, '-d', $Database, '-c', $sql) 120
    if ($result.ExitCode -ne 0) { Write-Warning "Počty řádků PG: psql skončil $($result.ExitCode): $($result.Err.Trim())"; return $null }
    $counts = @{}
    foreach ($line in $result.Out -split "`r?`n") {
        if ($line -match '^([^|]+)\|(\d+)$') { $counts[$matches[1]] = [long]$matches[2] }
    }
    if ($counts.Count -eq 0) { Write-Warning 'Počty řádků PG: prázdný výsledek.'; return $null }
    $counts
}

# Rozhodnutí hlídače Dockeru (#1552, varianta B) — čistá funkce kvůli testům:
#   ok        daemon odpovídá
#   skip-busy běží úloha, která Docker restartuje sama (deploy, kompaktace, aktualizace)
#   hung      Docker Desktop hlásí running, ale daemon neodpovídá → jen upozornit;
#             restart podle rozhodnutí vlastníka jen při stavu ≠ running
#   wait      přechodový stav (starting, stopping…) kratší než TransitionMinutes
#   restart   stopped / CLI neodpovídá / přechodový stav trvá
function Resolve-WatchdogAction {
    param(
        [bool]$InfoOk,
        [string]$Status,
        [string[]]$Busy = @(),
        [double]$IncidentMinutes = 0,
        [double]$TransitionMinutes = 10
    )
    if ($InfoOk) { return 'ok' }
    if (@($Busy | Where-Object { $_ }).Count -gt 0) { return 'skip-busy' }
    if ($Status -eq 'running') { return 'hung' }
    if (-not $Status -or $Status -in 'stopped', 'unknown') { return 'restart' }
    if ($IncidentMinutes -ge $TransitionMinutes) { return 'restart' }
    'wait'
}

# Tabulky, kterým po zásahu ubyly řádky nebo zmizely (nové tabulky a přírůstky
# z běžícího enginu a news-enginu jsou v pořádku) — prázdné pole = beze ztráty
function Compare-PgRowCounts([hashtable]$Before, [hashtable]$After) {
    @(foreach ($table in ($Before.Keys | Sort-Object)) {
        if (-not $After.ContainsKey($table)) { "$table (chybí)" }
        elseif ($After[$table] -lt $Before[$table]) { "$table ($($Before[$table]) → $($After[$table]))" }
    })
}
