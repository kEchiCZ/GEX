# Docker a WSL na hostiteli — sdílené funkce provozních skriptů, které Docker
# zastavují, restartují nebo aktualizují (kompaktace VHDX #1127, aktualizace WSL
# a Docker Desktop v pauze a hlídač Dockeru #1552). Jedna definice místo kopií.
#
# Použití:  . (Join-Path $PSScriptRoot 'lib\GlobexClock.ps1')
#           . (Join-Path $PSScriptRoot 'lib\DockerHost.ps1')

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
    $running = @(Get-GexRunning)
    $missing = @($Expected | Where-Object { $_ -notin $running })
    if ($missing.Count -eq 0) { return @() }
    $order = @($missing | Sort-Object { $_ -notlike '*postgres*' }, { $_ })
    Write-Warning "Neběží $($order -join ', ') — zkouším docker start."
    $result = Invoke-Docker (@('start') + $order) 120
    if ($result.ExitCode -ne 0) { Write-Warning "docker start skončil kódem $($result.ExitCode): $($result.Err.Trim())" }
    Start-Sleep -Seconds 20
    $running = @(Get-GexRunning)
    @($Expected | Where-Object { $_ -notin $running })
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
# deploy recreatuje engine, walk-forward a záloha PG čtou Postgres. Vlastní
# proces se nepočítá. Elevované úlohy (kompaktace, aktualizace) tudy NEJDOU
# vidět: neelevovaný proces jejich příkazový řádek nepřečte (Win32_Process
# vrátí prázdný, ověřeno 13. 10. 2026) — ty drží zámek údržby níže.
function Get-BusyJobs([string]$Pattern) {
    @(Get-CimInstance Win32_Process -Filter "Name='pwsh.exe' OR Name='powershell.exe'" -ErrorAction SilentlyContinue |
        ForEach-Object { if ($_.ProcessId -ne $PID -and $_.CommandLine -match $Pattern) { $matches[0] } } |
        Sort-Object -Unique)
}

# Zámek údržby Dockeru (#1552): kompaktace VHDX a aktualizace WSL / Docker
# Desktopu se nesmí potkat (diskpart drží datový VHDX, instalátor restartuje VM)
# a hlídač je musí poznat i jako neelevovaný proces. Soubor nese jméno úlohy
# a PID; bere se až po čekání na ostatní úlohy (dvě čekající úlohy se tak
# navzájem neblokují) a jen jednou (CreateNew). Zámek mrtvého procesu (úlohu
# zabil Task Scheduler) nebo starší než MaxHours se při čtení smaže.
function Get-MaintenanceLockPath { Join-Path (Split-Path -Parent (Get-OpsLogDir)) 'docker-maintenance.lock' }

# Jméno úlohy, která zámek drží; $null = volno
function Get-MaintenanceLockHolder([double]$MaxHours = 3) {
    $path = Get-MaintenanceLockPath
    if (-not (Test-Path $path)) { return $null }
    try { $lock = Get-Content $path -Raw | ConvertFrom-Json } catch { $lock = $null }
    $alive = $lock -and $lock.pid -and (Get-Process -Id ([int]$lock.pid) -ErrorAction SilentlyContinue) -and
        ((Get-Date) - [datetime]$lock.since).TotalHours -lt $MaxHours
    if ($alive) { return [string]$lock.name }
    $who = if ($lock) { "$($lock.name), PID $($lock.pid), od $($lock.since)" } else { 'nečitelný' }
    Write-Step "Zámek údržby bez živého držitele ($who) — odstraňuji."
    Remove-Item $path -Force -ErrorAction SilentlyContinue
    $null
}

# $true = zámek získán; $false = drží ho jiná úloha
function Enter-MaintenanceLock([string]$Name) {
    $path = Get-MaintenanceLockPath
    [void](Get-MaintenanceLockHolder)  # uklidí zámek mrtvého držitele
    try { $stream = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read) }
    catch [IO.IOException] { if (Test-Path $path) { return $false } else { throw } }
    try {
        $json = @{ name = $Name; pid = $PID; since = (Get-Date).ToString('s') } | ConvertTo-Json -Compress
        $bytes = [Text.Encoding]::UTF8.GetBytes($json)
        $stream.Write($bytes, 0, $bytes.Length)
    } finally { $stream.Dispose() }
    $true
}

function Exit-MaintenanceLock {
    $path = Get-MaintenanceLockPath
    try { $lock = Get-Content $path -Raw -ErrorAction Stop | ConvertFrom-Json } catch { return }
    if ([int]$lock.pid -eq $PID) { Remove-Item $path -Force -ErrorAction SilentlyContinue }
}

# Čeká, až neběží úlohy z $Pattern ani jiná údržba, a vezme zámek; $false = po
# $Minutes pořád obsazeno (úloha to zkusí příště). Volající zámek uvolní ve finally.
function Wait-MaintenanceSlot([string]$Name, [string]$Pattern, [int]$Minutes) {
    $deadline = (Get-Date).AddMinutes($Minutes)
    while ($true) {
        $busy = @(Get-BusyJobs $Pattern) + @(Get-MaintenanceLockHolder | Where-Object { $_ })
        if ($busy.Count -eq 0 -and (Enter-MaintenanceLock $Name)) { return $true }
        if ((Get-Date) -gt $deadline) { Write-Step "Pořád běží $($busy -join ', ') ani po $Minutes min."; return $false }
        Write-Step "Běží $($busy -join ', ') — čekám..."
        Start-Sleep -Seconds 15
    }
}

# Je trh zavřený? Zdroj pravdy je compute/marketclock.is_market_closed (AGENTS.md):
# zná svátky CME — Velký pátek, Vánoce, Nový rok, polední zastavení o US svátku —
# které Get-GlobexClock nezná. Volá se přes Python z .venv repa; když chybí nebo
# selže, náhradou Get-GlobexClock (svátek pak vyjde jako otevřený trh) s varováním.
function Test-MarketClosed([DateTime]$UtcNow = [DateTime]::UtcNow) {
    $repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $python = @('.venv\Scripts\python.exe', '.venv/bin/python') | ForEach-Object { Join-Path $repo $_ } |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    $iso = [DateTime]::SpecifyKind($UtcNow, 'Utc').ToString("yyyy-MM-dd'T'HH:mm:ss'+00:00'")
    if ($python) {
        $code = 'import datetime as dt, sys; from gexlens_engine.compute.marketclock import is_market_closed; ' +
            'print(int(is_market_closed(dt.datetime.fromisoformat(sys.argv[1]))))'
        $result = Invoke-Native $python @('-c', $code, $iso) 60
        $out = "$($result.Out)".Trim()
        if ($result.ExitCode -eq 0 -and $out -in '0', '1') { return $out -eq '1' }
        Write-Warning "is_market_closed přes $python selhal ($($result.ExitCode)): $("$($result.Err)".Trim()) — náhradou Get-GlobexClock bez svátků."
    } else {
        Write-Warning "Python v .venv repa ($repo) nenalezen — náhradou Get-GlobexClock bez svátků."
    }
    (Get-GlobexClock -UtcNow $UtcNow).Closed
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

# Tabulky, které po zásahu zmizely nebo jim ubylo víc než MaxDropShare řádků —
# prázdné pole = beze ztráty. Smazaný datový disk WSL = prázdné nebo chybějící
# tabulky; malý pokles je běžný (přepočty DELETE+INSERT: news_model_stats,
# track_record, sentiment_waves…), proto výchozí práh polovina. -MaxDropShare 0
# vrátí každý pokles (jen do logu). Nové tabulky a přírůstky jsou v pořádku.
function Compare-PgRowCounts([hashtable]$Before, [hashtable]$After, [double]$MaxDropShare = 0.5) {
    @(foreach ($table in ($Before.Keys | Sort-Object)) {
        if (-not $After.ContainsKey($table)) { "$table (chybí)" }
        elseif ($After[$table] -lt $Before[$table] -and $After[$table] -le $Before[$table] * (1 - $MaxDropShare)) {
            "$table ($($Before[$table]) → $($After[$table]))"
        }
    })
}
