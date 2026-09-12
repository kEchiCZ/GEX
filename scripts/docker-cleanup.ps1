# Bezpečný úklid Dockeru (#1127) — rollback tagy, build cache, osiřelé image, hlídka místa.
#
# Proč: 12. 9. 2026 měl VHDX Dockeru 71 GB (build cache 43 GB z 507 buildů,
# 36 rollback tagů `gex-*:pre-<issue>` z deploy skriptu), build zaplnil disk D:
# na 0 B, containerd ve VM spadl a Docker Desktop se nerozjel.
#
# Co se maže (vše jde znovu vyrobit buildem):
#   1. rollback tagy `gex-*:pre-*` kromě N nejnovějších na službu,
#   2. build cache starší než -CacheAgeHours,
#   3. osiřelé (dangling) image.
# Co se NIKDY nemaže: volumes (`pgdata` = Postgres), aktuální tagované image
# (`latest`, dev image). `docker volume prune` sem nepatří ani v budoucnu.
#
# Hlídka místa: když je na disku s VHDX méně než -MinFreeGB nebo VHDX větší
# než -MaxVhdxGB, pošle alert do zvonku aplikace (kanál `alerts`, jako
# provozní alerty enginu) s doporučením kompakce (`compact-docker-vhdx.ps1`,
# správce, zastaví Docker — proto se nespouští automaticky).
#
# Volání: z deploy skriptu po úspěšném nasazení, nebo týdně z Task Scheduleru
# (`register-docker-cleanup-task.ps1`, sobota ráno — trh zavřený).
[CmdletBinding()]
param(
    # Kolik nejnovějších `pre-*` tagů na službu ponechat
    [int]$KeepTags = 3,
    # Build cache starší než tolik hodin se maže (168 = 7 dní)
    [int]$CacheAgeHours = 168,
    # Práh volného místa na disku s VHDX (GB) pro alert
    [double]$MinFreeGB = 15,
    # Práh velikosti VHDX (GB) pro alert — nad ním dává smysl kompakce
    [double]$MaxVhdxGB = 40,
    # Jen vypsat, nic nemazat
    [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
. (Join-Path $PSScriptRoot '_docker.ps1')

function Write-Step($text) { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $text" }

Assert-DockerReady

# ── 1) Rollback tagy: nechat jen $KeepTags nejnovějších na službu ───────
$services = @('gex-engine', 'gex-api', 'gex-news-engine', 'gex-frontend')
$removedTags = 0
foreach ($svc in $services) {
    $tags = docker images --format '{{.CreatedAt}}|{{.Repository}}:{{.Tag}}' |
        Where-Object { $_ -match "\|$([regex]::Escape($svc)):pre-" } |
        Sort-Object -Descending |
        ForEach-Object { $_.Split('|')[1] }
    $old = @($tags | Select-Object -Skip $KeepTags)
    foreach ($tag in $old) {
        if ($WhatIf) { Write-Step "WhatIf: smazal bych $tag"; continue }
        docker rmi $tag *> $null
        if ($LASTEXITCODE -eq 0) { $removedTags++; Write-Step "Smazán rollback tag $tag" }
        else { Write-Warning "Tag $tag se nepodařilo smazat (nejspíš ho používá kontejner) — nechávám." }
    }
}
Write-Step "Rollback tagy: smazáno $removedTags, ponecháno ≤ $KeepTags na službu."

# ── 2) Build cache starší než $CacheAgeHours ─────────────────────────────
if ($WhatIf) {
    Write-Step "WhatIf: docker builder prune --filter until=${CacheAgeHours}h"
} else {
    $out = docker builder prune -f --filter "until=${CacheAgeHours}h" 2>&1 | Out-String
    $line = ($out -split "`n" | Where-Object { $_ -match 'reclaimed' } | Select-Object -Last 1)
    Write-Step "Build cache (> $CacheAgeHours h): $($line.Trim())"
}

# ── 3) Osiřelé image ─────────────────────────────────────────────────────
if ($WhatIf) {
    Write-Step 'WhatIf: docker image prune -f'
} else {
    $out = docker image prune -f 2>&1 | Out-String
    $line = ($out -split "`n" | Where-Object { $_ -match 'reclaimed' } | Select-Object -Last 1)
    Write-Step "Osiřelé image: $($line.Trim())"
}

# ── 4) Report: docker system df + VHDX + volné místo ────────────────────
docker system df | ForEach-Object { Write-Host "  $_" }

# VHDX Docker Desktopu: cesta z nastavení (dataFolder), jinak výchozí umístění
$vhdx = $null
$settingsPath = Join-Path $env:APPDATA 'Docker\settings-store.json'
if (-not (Test-Path $settingsPath)) { $settingsPath = Join-Path $env:APPDATA 'Docker\settings.json' }
if (Test-Path $settingsPath) {
    try {
        $dataFolder = (Get-Content $settingsPath -Raw | ConvertFrom-Json).dataFolder
        if ($dataFolder) {
            $vhdx = Get-ChildItem $dataFolder -Recurse -Filter 'docker_data.vhdx' -ErrorAction SilentlyContinue | Select-Object -First 1
        }
    } catch { }
}
if (-not $vhdx) {
    $vhdx = @(
        'D:\Programy\Docker\DockerDesktopWSL\disk\docker_data.vhdx',
        (Join-Path $env:LOCALAPPDATA 'Docker\wsl\disk\docker_data.vhdx'),
        (Join-Path $env:LOCALAPPDATA 'Docker\wsl\data\ext4.vhdx')
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1 | ForEach-Object { Get-Item $_ }
}

$problems = @()
if ($vhdx) {
    $vhdxGB = [math]::Round($vhdx.Length / 1GB, 1)
    $drive = Get-PSDrive ($vhdx.FullName.Substring(0, 1))
    $freeGB = [math]::Round($drive.Free / 1GB, 1)
    Write-Step "VHDX $($vhdx.FullName): $vhdxGB GB; disk $($drive.Name): volných $freeGB GB"
    if ($freeGB -lt $MinFreeGB) { $problems += "na disku $($drive.Name): zbývá $freeGB GB (práh $MinFreeGB GB)" }
    if ($vhdxGB -gt $MaxVhdxGB) { $problems += "VHDX Dockeru má $vhdxGB GB (práh $MaxVhdxGB GB) — spusť jako správce scripts/compact-docker-vhdx.ps1" }
} else {
    Write-Warning 'VHDX Dockeru nenalezen — hlídka místa přeskočena.'
}

# ── 5) Alert do zvonku aplikace při nedostatku místa ────────────────────
# Stejný kanál a tvar jako provozní alerty enginu (`connection_stall`,
# `strikes_stalled`): POST /internal/publish {channel: alerts, data: {...}}
# s hlavičkou X-GEXLens-Token; token se čte z prostředí nebo .env a nikdy
# se nevypisuje.
if ($problems.Count -gt 0) {
    $message = "Docker: " + ($problems -join '; ') + ". Plný disk shodí containerd i sběr dat (12. 9. 2026)."
    Write-Warning $message
    $token = $env:GEXLENS_API_TOKEN
    if (-not $token) {
        $line = Get-Content .env -ErrorAction SilentlyContinue |
            Where-Object { $_ -match '^GEXLENS_API_TOKEN=' } | Select-Object -First 1
        if ($line) { $token = $line.Substring('GEXLENS_API_TOKEN='.Length).Trim('"') }
    }
    if ($token) {
        $body = @{
            channel = 'alerts'
            data    = @{
                kind    = 'disk_low'
                symbol  = '*'
                message = $message
                ts      = [double][DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000
            }
        } | ConvertTo-Json -Depth 4
        try {
            Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/internal/publish' `
                -Headers @{ 'X-GEXLens-Token' = $token } -ContentType 'application/json' -Body $body | Out-Null
            Write-Step 'Alert disk_low odeslán do zvonku.'
        } catch {
            Write-Warning "Alert se nepodařilo odeslat (API neběží?): $($_.Exception.Message)"
        }
    } else {
        Write-Warning 'GEXLENS_API_TOKEN není v prostředí ani v .env — alert jen do logu.'
    }
    exit 2
}
Write-Step 'Úklid hotov, místo v pořádku.'
