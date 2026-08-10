# Start PRODUKČNÍHO stacku GEXLens (#568).
#
# Pojistky:
#  * Běží-li dev engine (režim dev-live), nejdřív celý dev stack shodí —
#    jeden market data účet neunese dva enginy (exkluzivita, #568).
#  * S -Build odmítne stavět z jiné větve než main nebo ze špinavého stromu
#    (produkce pouští výhradně mergnutý kód). Bez -Build se jen startují už
#    postavené image, tam na větvi nezáleží. Override: -Force.
#
# Použití:
#   .\scripts\start-prod.ps1          # start (bez rebuildů)
#   .\scripts\start-prod.ps1 -Build   # nasazení po mergi (rebuild z main)
[CmdletBinding()]
param(
    [switch]$Build,
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

if ($Build -and -not $Force) {
    $branch = (git rev-parse --abbrev-ref HEAD 2>$null)
    $dirty = (git status --porcelain 2>$null)
    if ($branch -ne 'main') {
        throw "Produkce se staví jen z main (jsi na '$branch'). Přepni větev, nebo vědomě obejdi přes -Force."
    }
    if ($dirty) {
        throw "Pracovní strom není čistý — build by zapekl nezacommitované změny do produkce. Ukliď, nebo -Force."
    }
}

# Exkluzivita s dev-live: dev bez enginu smí běžet dál (účtu se nedotkne)
$devEngine = docker ps --filter 'name=gexdev-engine' --format '{{.Names}}'
if ($devEngine) {
    Write-Host 'Běží DEV s enginem — shazuji dev stack (jeden market data účet)…' -ForegroundColor Yellow
    docker compose -f compose.dev.yml --profile live down
}

$composeArgs = @('compose', 'up', '-d')
if ($Build) { $composeArgs += '--build' }
Write-Host 'Startuji PROD…' -ForegroundColor Cyan
docker @composeArgs
if ($LASTEXITCODE -ne 0) { throw 'docker compose selhal — běží Docker Desktop?' }

# Čekání na frontend a otevření prohlížeče (zrcadlí původní start-gexlens.cmd)
$url = 'http://127.0.0.1:8080/'
for ($i = 0; $i -lt 60; $i++) {
    try {
        Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null
        Start-Process $url
        Write-Host "PROD běží: $url" -ForegroundColor Green
        return
    } catch { Start-Sleep -Seconds 2 }
}
throw "Aplikace nenaběhla do 2 minut — zkontroluj 'docker compose logs'."
