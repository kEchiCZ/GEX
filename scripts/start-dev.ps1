# Start DEV stacku GEXLens (#568) — vlastní PG volume, data-dev/, porty +1.
#
# Bez -Live:     postgres + api + frontend. Market data účtu se nedotkne,
#                produkce smí běžet dál (živý sběr se nepřerušuje).
# S -Live:       + engine + news-engine. Jeden účet → NEJDŘÍV SHODÍ PRODUKCI
#                (exkluzivitu vynucuje skript, ne disciplína).
# S -LiveTasty:  + engine JEN s tastytrade (#623) — IBKR vypnuto, produkce
#                běží dál (tasty snese souběžné streamy, ADR-0027). Laboratoř
#                providera/symbologie; cross-feed logika se tu ověřit nedá.
#
# Data: kopie produkce přes scripts/seed-dev.ps1 (spusť před prvním použitím).
#
# Použití:
#   .\scripts\start-dev.ps1                 # dev bez enginu
#   .\scripts\start-dev.ps1 -Build          # + rebuild image z aktuální větve
#   .\scripts\start-dev.ps1 -Live -Build    # plný stack proti TWS
#   .\scripts\start-dev.ps1 -LiveTasty      # tasty-only engine, prod nedotčena
[CmdletBinding()]
param(
    [switch]$Live,
    [switch]$LiveTasty,
    [switch]$Build
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot '_docker.ps1')

if ($Live -and $LiveTasty) { throw '-Live a -LiveTasty se vylučují: buď plný stack proti TWS, nebo tasty laboratoř.' }

# Flag se čte i z lokálního prostředí (compose interpolace) — bez -LiveTasty
# ho vždy vynulovat, jinak by po tasty běhu „zůstal zapnutý" i pro -Live
$env:GEXLENS_TASTY_ONLY = if ($LiveTasty) { 'true' } else { 'false' }
if ($LiveTasty -and -not $env:GEXLENS_TASTY_MAX_SUBSCRIPTIONS) {
    # Konzervativní dev strop (#623): měřeno 6 008 symbolů bez degradace
    # (ADR-0027); dev si bere třetinu, aby neujídal kapacitu účtu produkci.
    # Produkční hodnoty se NEdotýká — ta jede na maximum.
    $env:GEXLENS_TASTY_MAX_SUBSCRIPTIONS = '2000'
}

if ($Live) {
    $prod = docker compose ps --services --status running 2>$null
    if ($prod) {
        Write-Host 'Režim dev-live: shazuji PRODUKCI (jeden market data účet)…' -ForegroundColor Yellow
        Write-Host 'POZOR: po dobu vývoje produkce nesbírá — v datech vznikne díra (#221).' -ForegroundColor Yellow
        Invoke-DockerChecked -Arguments @('compose', 'down') `
            -FailureHint 'Shození produkce selhalo — nepouštím dev engine proti běžícímu prod.'
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
# -LiveTasty startuje engine výslovně jmenovaný (profil není potřeba) a BEZ
# news-engine — ten mluví s TWS a srazil by se s produkcí
if ($LiveTasty) { $composeArgs += @('postgres', 'api', 'frontend', 'engine') }

Assert-DockerReady
$branch = (git rev-parse --abbrev-ref HEAD 2>$null)
Write-Host "Startuji DEV$(if ($Live) { ' + engine (live)' } elseif ($LiveTasty) { ' + engine (tasty-only)' }) z větve '$branch'…" -ForegroundColor Cyan
Invoke-DockerChecked -Arguments $composeArgs -FailureHint 'Start dev stacku selhal.'

$url = 'http://127.0.0.1:8081/'
for ($i = 0; $i -lt 60; $i++) {
    try {
        Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null
        Start-Process $url
        Write-Host "DEV běží: $url (produkce: $(if ($Live) { 'ZASTAVENA' } else { 'nedotčena' })$(if ($LiveTasty) { '; engine tasty-only — heartbeat v logu enginu' }))" -ForegroundColor Green
        return
    } catch { Start-Sleep -Seconds 2 }
}
throw "DEV nenaběhl do 2 minut — zkontroluj 'docker compose -f compose.dev.yml logs'."
