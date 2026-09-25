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
    # Rezerva do otevření Globexu: kompaktace ~1,5 min, nejhůř ~11 min (diskpart + start Dockeru)
    [int]$MinMinutesToOpen = 20,
    # Jak dlouho čekat na doběhnutí deploye / walk-forward / zálohy PG
    [int]$BusyWaitMinutes = 20
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\GlobexClock.ps1')
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

$marketState = if ($clock.Closed) { 'trh zavřený' } else { 'trh OTEVŘENÝ, -Force' }
Write-Step "Zastavuji Docker Desktop a WSL ($marketState, CT $($clock.Label))..."
# Docker se musí znovu spustit i při chybě uprostřed (#1277) — jinak sběr
# stojí přes otevření trhu a alert nejde poslat (API běží v Dockeru)
try {
    Get-Process 'Docker Desktop', com.docker.backend -ErrorAction SilentlyContinue | Stop-Process -Force -Confirm:$false -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 5
    wsl --shutdown
    Start-Sleep -Seconds 5

    Write-Step ("VHDX před: {0:N1} GB" -f $before)

    $script = @"
select vdisk file="$vhdx"
attach vdisk readonly
compact vdisk
detach vdisk
exit
"@
    $tmp = Join-Path $env:TEMP 'compact-vhdx.txt'
    Set-Content -Path $tmp -Value $script -Encoding ascii
    diskpart /s $tmp
    Remove-Item $tmp -ErrorAction SilentlyContinue

    $after = (Get-Item $vhdx).Length / 1GB
    Write-Step ("VHDX po:   {0:N1} GB (uvolněno {1:N1} GB)" -f $after, ($before - $after))
} finally {
    Write-Step 'Startuji Docker Desktop...'
    Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
}

# Kontejnery s restart: unless-stopped naběhnou samy; ověřit, že se tak stalo,
# jinak by sběr stál až do rána (26. 8. 2026: pád daemonu = 23 min díra)
$deadline = (Get-Date).AddMinutes(5)
$ready = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 10
    try { docker info *> $null; if ($LASTEXITCODE -eq 0) { $ready = $true; break } } catch { }
}
if (-not $ready) { Write-Warning 'Docker do 5 minut nenaběhl — zkontroluj Docker Desktop ručně.'; exit 1 }
Start-Sleep -Seconds 20
$running = @(docker ps --filter 'name=gex-' --format '{{.Names}}' 2>$null)
Write-Step "Docker běží; kontejnery gex-*: $($running.Count) ($($running -join ', '))"
if ($running.Count -lt 5) { Write-Warning 'Čekal jsem 5 kontejnerů gex-* — některý nenaběhl.'; exit 1 }
