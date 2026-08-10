# Start DEV stacku GEXLens (#568) — vlastní PG volume, data-dev/, porty +1.
#
# Bez -Live:  postgres + api + frontend. Market data účtu se nedotkne,
#             produkce smí běžet dál (živý sběr se nepřerušuje).
# S -Live:    + engine + news-engine. Jeden účet → NEJDŘÍV SHODÍ PRODUKCI
#             (exkluzivitu vynucuje skript, ne disciplína).
#
# Data: kopie produkce přes scripts/seed-dev.ps1 (spusť před prvním použitím).
#
# Použití:
#   .\scripts\start-dev.ps1                 # dev bez enginu
#   .\scripts\start-dev.ps1 -Build          # + rebuild image z aktuální větve
#   .\scripts\start-dev.ps1 -Live -Build    # plný stack proti TWS
[CmdletBinding()]
param(
    [switch]$Live,
    [switch]$Build
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

if ($Live) {
    $prod = docker compose ps --services --status running 2>$null
    if ($prod) {
        Write-Host 'Režim dev-live: shazuji PRODUKCI (jeden market data účet)…' -ForegroundColor Yellow
        Write-Host 'POZOR: po dobu vývoje produkce nesbírá — v datech vznikne díra (#221).' -ForegroundColor Yellow
        docker compose down
        if ($LASTEXITCODE -ne 0) { throw 'Shození produkce selhalo — nepouštím dev engine proti běžícímu prod.' }
    }
}

if (-not (Test-Path 'data-dev')) {
    Write-Warning "data-dev/ neexistuje — dev poběží bez parquet dat. Naplň kopií produkce: .\scripts\seed-dev.ps1"
    New-Item -ItemType Directory -Path 'data-dev' | Out-Null
}

$composeArgs = @('compose', '-f', 'compose.dev.yml')
if ($Live) { $composeArgs += @('--profile', 'live') }
$composeArgs += @('up', '-d')
if ($Build) { $composeArgs += '--build' }

$branch = (git rev-parse --abbrev-ref HEAD 2>$null)
Write-Host "Startuji DEV$(if ($Live) { ' + engine (live)' }) z větve '$branch'…" -ForegroundColor Cyan
docker @composeArgs
if ($LASTEXITCODE -ne 0) { throw 'docker compose selhal — běží Docker Desktop?' }

$url = 'http://127.0.0.1:8081/'
for ($i = 0; $i -lt 60; $i++) {
    try {
        Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null
        Start-Process $url
        Write-Host "DEV běží: $url (produkce: $(if ($Live) { 'ZASTAVENA' } else { 'nedotčena' }))" -ForegroundColor Green
        return
    } catch { Start-Sleep -Seconds 2 }
}
throw "DEV nenaběhl do 2 minut — zkontroluj 'docker compose -f compose.dev.yml logs'."
