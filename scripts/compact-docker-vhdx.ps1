# Zkomprimuje VHDX Docker Desktopu (D:\Programy\Docker\DockerDesktopWSL\disk\docker_data.vhdx).
# VHDX po smazání dat uvnitř VM sám nezmenší — místo se vrátí až kompakcí.
# SPUSTIT JAKO SPRÁVCE (diskpart to vyžaduje). Docker Desktop se na 2–5 min zastaví.
#
#   pwsh -File scripts/compact-docker-vhdx.ps1              ruční běh (#1127; 12. 9. 2026: 70,9 → 24,9 GB)
#   pwsh -File scripts/compact-docker-vhdx.ps1 -IfNeeded    jen když VHDX > MinVhdxGB a uvnitř je ≥ MinReclaimGB volných
#   pwsh -File scripts/compact-docker-vhdx.ps1 -IfNeeded -DryRun   jen rozhodnutí, nic nezastaví
#
# Automatika (#1127, 24. 9. 2026): úloha „GEXLens compact-vhdx" (register-compact-vhdx-task.ps1,
# elevovaná, po/st/pá 23:05 + sobota 10:00, #1277) + docker-cleanup.ps1 ji spouští po úklidu image, když je
# VHDX nad prahem. Kompaktace NIC nemaže — jen vrací Windows místo, které je uvnitř
# VHDX už volné; retenci záloh řídí docker-cleanup (-KeepTags) a backup-postgres (-Keep).
#
# Trh musí být zavřený (pauza Globexu 16:00–17:00 CT, víkend) — Docker a s ním sběr
# dat stojí; brána se přeskočí jen s -Force. Do otevření musí zbývat aspoň
# MinMinutesToOpen a nesmí běžet deploy / walk-forward / záloha PG (#1277):
# spouštěč po/st/pá 23:05 padá do okna deploye (engine se recreatuje 23:04–23:08),
# takže se na ně čeká, ne přeruší — deploy si kompaktaci stejně spouští sám na konci.
[CmdletBinding()]
param(
    # Cesta k datovému disku Docker Desktopu (Settings → Resources → Disk image location)
    [string]$Vhdx = 'D:\Programy\Docker\DockerDesktopWSL\disk\docker_data.vhdx',
    # Kompaktovat jen při překročení prahů (automatický režim)
    [switch]$IfNeeded,
    [double]$MinVhdxGB = 40,
    [double]$MinReclaimGB = 10,
    # Jen vypsat rozhodnutí
    [switch]$DryRun,
    # Přeskočit bránu obchodních hodin (jen vědomě, ručně)
    [switch]$Force,
    # Rezerva do otevření Globexu: kompaktace ~1,5 min; nejhůř ~17 min (diskpart, 5 min
    # čekání na Docker, jeden restart, dalších 5 min, docker start)
    [int]$MinMinutesToOpen = 20,
    # Jak dlouho čekat na doběhnutí deploye / walk-forward / zálohy PG
    [int]$BusyWaitMinutes = 20
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\GlobexClock.ps1')
. (Join-Path $PSScriptRoot 'lib\OpsAlert.ps1')
function Write-Step($text) { Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $text" }

$vhdx = $Vhdx
if (-not (Test-Path $vhdx)) { throw "VHDX nenalezen: $vhdx" }
$before = (Get-Item $vhdx).Length / 1GB

# ── Brána prahů: co uvnitř VHDX Docker skutečně používá vs. velikost souboru ──
if ($IfNeeded) {
    if ($before -lt $MinVhdxGB) {
        Write-Step ("VHDX {0:N1} GB je pod prahem {1} GB — kompaktace není třeba." -f $before, $MinVhdxGB)
        exit 0
    }
    # `docker system df` sečte image + kontejnery + svazky + build cache; rozdíl
    # proti velikosti souboru je to, co kompaktace vrátí (odhad, ne přesná hodnota)
    $used = 0.0
    try {
        $rows = docker system df --format '{{.Size}}' 2>$null
        foreach ($row in $rows) {
            if ($row -match '^([\d.]+)\s*(B|kB|MB|GB|TB)$') {
                $n = [double]$matches[1]
                $used += switch ($matches[2]) { 'B' { $n / 1GB } 'kB' { $n / 1MB } 'MB' { $n / 1KB } 'GB' { $n } 'TB' { $n * 1KB } }
            }
        }
    } catch { Write-Warning "docker system df selhal ($_) — bez odhadu uvnitř, rozhoduje jen velikost VHDX." }
    $reclaim = $before - $used
    Write-Step ("VHDX {0:N1} GB, Docker uvnitř používá ~{1:N1} GB → k vrácení ~{2:N1} GB (práh {3} GB)." -f $before, $used, $reclaim, $MinReclaimGB)
    if ($used -gt 0 -and $reclaim -lt $MinReclaimGB) {
        Write-Step 'Uvnitř není dost volného místa — kompaktace by nic nevrátila, končím.'
        exit 0
    }
}

# Souběžné úlohy, které Docker potřebují (#1277): deploy recreatuje engine
# (zastavení Dockeru uprostřed = rozjeté verze nebo engine bez startu přes
# otevření trhu), walk-forward a záloha PG čtou Postgres
function Get-BusyJobs {
    @(Get-CimInstance Win32_Process -Filter "Name='pwsh.exe' OR Name='powershell.exe'" -ErrorAction SilentlyContinue |
        ForEach-Object { if ($_.ProcessId -ne $PID -and $_.CommandLine -match 'deploy-engine-offhours|walkforward-nightly|backup-postgres') { $matches[0] } } |
        Sort-Object -Unique)
}
if ($DryRun) {
    $busy = Get-BusyJobs
    if ($busy.Count -gt 0) { Write-Step "DryRun: běží $($busy -join ', ') — kompaktace by počkala." }
} else {
    $busyDeadline = (Get-Date).AddMinutes($BusyWaitMinutes)
    while (($busy = Get-BusyJobs).Count -gt 0) {
        if ((Get-Date) -gt $busyDeadline) {
            Write-Step "Pořád běží $($busy -join ', ') ani po $BusyWaitMinutes min — kompaktaci vzdávám, zkusí se příště."
            exit 0
        }
        Write-Step "Běží $($busy -join ', ') — čekám, Docker teď zastavit nejde..."
        Start-Sleep -Seconds 15
    }
}

# Brána trhu až po čekání — čas mezitím utekl
$clock = Get-GlobexClock
if (-not $Force) {
    if (-not $clock.Closed) {
        Write-Step "Trh je otevřený (CT $($clock.Label)) — kompaktace zastaví sběr, odkládám. (-Force jen vědomě.)"
        exit 0
    }
    if ($clock.MinutesToOpen -lt $MinMinutesToOpen) {
        Write-Step ("Do otevření Globexu zbývá {0:N0} min (< {1}) — kompaktace by mohla přetéct přes otevření, odkládám." -f $clock.MinutesToOpen, $MinMinutesToOpen)
        exit 0
    }
}
if ($DryRun) { Write-Step 'DryRun: kompaktace by teď proběhla.'; exit 0 }

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Spusť jako správce (diskpart vyžaduje elevaci) — nebo přes úlohu „GEXLens compact-vhdx" (register-compact-vhdx-task.ps1).'
}

# Docker CLI s časovým limitem (#1279): při napůl živém daemonu `docker info`
# visí (lessons-learned: docker CLI při pádu lže/visí) a termín čekání by se
# nikdy nezkontroloval — úlohu by ve 60. minutě zabil Task Scheduler bez
# upozornění. Vypršení = daemon nereaguje.
function Invoke-Docker([string[]]$DockerArgs, [int]$TimeoutSec = 20) {
    $psi = [System.Diagnostics.ProcessStartInfo]::new('docker')
    foreach ($arg in $DockerArgs) { $psi.ArgumentList.Add($arg) }
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    try { $proc = [System.Diagnostics.Process]::Start($psi) }
    catch { return [pscustomobject]@{ ExitCode = -1; Out = ''; Err = $_.Exception.Message } }
    $out = $proc.StandardOutput.ReadToEndAsync()
    $err = $proc.StandardError.ReadToEndAsync()
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        try { $proc.Kill($true) } catch { }
        return [pscustomobject]@{ ExitCode = -1; Out = ''; Err = "docker $($DockerArgs[0]): bez odpovědi do $TimeoutSec s" }
    }
    [pscustomobject]@{ ExitCode = $proc.ExitCode; Out = $out.Result; Err = $err.Result }
}
# Běžící služby produkčního projektu (bez one-off kontejnerů typu gex-greeks-probe
# a bez crash loopu — status=running); prázdné i při nereagujícím daemonu
function Get-GexRunning {
    $result = Invoke-Docker @('ps', '--filter', 'label=com.docker.compose.project=gex',
        '--filter', 'label=com.docker.compose.oneoff=False', '--filter', 'status=running', '--format', '{{.Names}}')
    if ($result.ExitCode -ne 0) { return }  # nic = daemon nereaguje; @($null) by mělo Count 1
    @($result.Out -split "`r?`n" | Where-Object { $_ } | Sort-Object)
}
function Wait-DockerReady([int]$Minutes) {
    $deadline = (Get-Date).AddMinutes($Minutes)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 10
        if ((Invoke-Docker @('info') 20).ExitCode -eq 0) { return $true }
    }
    return $false
}
function Stop-DockerDesktop {
    Get-Process 'Docker Desktop', com.docker.backend -ErrorAction SilentlyContinue | Stop-Process -Force -Confirm:$false -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 5
    wsl --shutdown
    if ($LASTEXITCODE -ne 0) { Write-Warning "wsl --shutdown skončil kódem $LASTEXITCODE." }
    Start-Sleep -Seconds 5
}
function Start-DockerDesktop {
    Write-Step 'Startuji Docker Desktop...'
    Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
}
# Nativní diskpart výjimku nevyhodí — rozhoduje návratový kód (#1279)
function Invoke-Diskpart([string]$Commands) {
    $tmp = Join-Path $env:TEMP 'compact-vhdx.txt'
    Set-Content -Path $tmp -Value $Commands -Encoding ascii
    diskpart /s $tmp | Write-Host
    $code = $LASTEXITCODE
    Remove-Item $tmp -ErrorAction SilentlyContinue
    return $code
}

# Stav před kompaktací: po ní se očekává a případně spouští jen to, co běželo —
# záměrně zastavenou službu (docker compose stop) kompaktace nepustí
$expectedRunning = @(Get-GexRunning)
Write-Step "Před kompaktací běží: $($expectedRunning.Count) ($($expectedRunning -join ', '))"

$marketState = if ($clock.Closed) { 'trh zavřený' } else { 'trh OTEVŘENÝ, -Force' }
Write-Step "Zastavuji Docker Desktop a WSL ($marketState, CT $($clock.Label))..."
$compactError = $null
$problem = $null
# Vše od zastavení Dockeru je v try: neošetřená výjimka by jinak úlohu
# ukončila tiše, se zastaveným sběrem (#1279)
try {
    # Docker se musí znovu spustit i při chybě uprostřed (#1277)
    try {
        Stop-DockerDesktop
        Write-Step ("VHDX před: {0:N1} GB" -f $before)
        $code = Invoke-Diskpart "select vdisk file=`"$vhdx`"`nattach vdisk readonly`ncompact vdisk`ndetach vdisk`nexit"
        if ($code -ne 0) {
            $compactError = "diskpart skončil kódem $code"
            # diskpart /s končí na první chybě — `detach` se nemusel provést a
            # připojený VHDX by Docker nepustil
            Write-Warning "$compactError — odpojuji VHDX."
            [void](Invoke-Diskpart "select vdisk file=`"$vhdx`"`ndetach vdisk noerr`nexit")
        }
        $after = (Get-Item $vhdx).Length / 1GB
        Write-Step ("VHDX po:   {0:N1} GB (uvolněno {1:N1} GB)" -f $after, ($before - $after))
    } catch {
        $compactError = $_.Exception.Message
        Write-Warning "Kompaktace selhala: $compactError"
    } finally {
        Start-DockerDesktop
    }

    # Kontejnery s restart: unless-stopped naběhnou samy; ověřit, že se tak stalo,
    # jinak by sběr stál až do rána (26. 8. 2026: pád daemonu = 23 min díra).
    # Ve všední den zbývá do otevření < 1 h a uživatel spí — proto nejdřív
    # samooprava, teprve pak upozornění mimo Docker (#1279).
    $ready = Wait-DockerReady 5
    if (-not $ready) {
        Write-Warning 'Docker do 5 minut nenaběhl — zkouším jeden restart Docker Desktopu.'
        Stop-DockerDesktop
        Start-DockerDesktop
        $ready = Wait-DockerReady 5
    }
    $missing = @()
    if ($ready) {
        Start-Sleep -Seconds 20
        $running = @(Get-GexRunning)
        $missing = @($expectedRunning | Where-Object { $_ -notin $running })
        if ($missing.Count -gt 0) {
            # `docker start`, ne `compose up`: up by kontejner mohl recreatovat na
            # novější image z GHCR, než ho nasadí deploy. Postgres první.
            $order = @($missing | Sort-Object { $_ -notlike '*postgres*' }, { $_ })
            Write-Warning "Neběží $($order -join ', ') — zkouším docker start."
            $result = Invoke-Docker (@('start') + $order) 120
            if ($result.ExitCode -ne 0) { Write-Warning "docker start skončil kódem $($result.ExitCode): $($result.Err.Trim())" }
            Start-Sleep -Seconds 20
            $running = @(Get-GexRunning)
            $missing = @($expectedRunning | Where-Object { $_ -notin $running })
        }
        Write-Step "Docker běží; kontejnery gex-*: $($running.Count) ($($running -join ', '))"
    }
    $problem = if (-not $ready) {
        'Docker Desktop po kompaktaci VHDX nenaběhl ani po restartu — sběr dat stojí. Zkontroluj Docker Desktop.'
    } elseif ($missing.Count -gt 0) {
        "Po kompaktaci VHDX neběží $($missing -join ', ') — sběr dat může stát. Zkontroluj docker ps."
    } elseif ($compactError) {
        "Kompaktace VHDX selhala ($compactError); Docker znovu běží, místo se neuvolnilo."
    }
} catch {
    $problem = "Kompaktace VHDX spadla po zastavení Dockeru ($($_.Exception.Message)) — zkontroluj Docker Desktop a docker ps."
}
if ($problem) { Send-OpsAlert $problem; exit 1 }
