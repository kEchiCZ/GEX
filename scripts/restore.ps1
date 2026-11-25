# Obnova GEXLens ze zálohy jedním příkazem na PC (Docker Desktop), #1800.
#
# Zastaví zapisující služby (engine, news-engine, api; postgres běží dál), spustí
# `python -m gexlens_api.restore` v kontejneru api přes `docker compose run --rm`
# se zdrojem připojeným jen pro čtení, po úspěchu nastartuje stack (`up -d`)
# a ověří /status. Ověření zdroje, pojistka cíle i samotná obnova jsou
# v api/src/gexlens_api/restore.py (ADMIN-MANUAL kap. 14 „Obnova ze zálohy“).
#
# Výsledek obnovy: 0 = hotovo; 1 = cíl beze změny (služby se vrátí do stavu
# před obnovou); 2 = PG obnovená, data/ jen zčásti (služby zůstanou zastavené,
# obnovu spusť znovu s -Overwrite).
#
# Použití (z kořene repozitáře):
#   .\scripts\restore.ps1 -Source D:\Downloads\gexlens-zaloha-2026-11-25_2210-cd7f1d9.tar
#   .\scripts\restore.ps1 -Source D:\obnova\snimek -Overwrite       # cíl s daty
#   .\scripts\restore.ps1 -Source <zdroj> -ComposeFile compose.dev.yml   # dev stack (data-dev/, :8011)
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Source,
    [switch]$Overwrite,
    [string[]]$ComposeFile = @('compose.yml')
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

$item = Get-Item -LiteralPath $Source   # neexistující zdroj = chyba hned
$files = @($ComposeFile | ForEach-Object { '-f'; $_ })

function Invoke-Compose {
    & docker compose @files @args
    if ($LASTEXITCODE -ne 0) { throw "docker compose $($args -join ' ') selhal (kód $LASTEXITCODE)" }
}

# Jen zapisující služby, které právě běží: při selhání se vrátí přesně ty
$writers = 'engine', 'news-engine', 'api'
$running = @(& docker compose @files ps --services --status running | Where-Object { $writers -contains $_ })
if ($LASTEXITCODE -ne 0) { throw "docker compose ps selhal (kód $LASTEXITCODE)" }

Invoke-Compose up -d --wait postgres
$name = if ($item.PSIsContainer) { 'zdroj' } else { $item.Name }
$inside = "/restore/$name"
$restoreArgs = @('python', '-m', 'gexlens_api.restore', $inside)
if ($Overwrite) { $restoreArgs += '--overwrite' }

if ($running.Count) {
    Write-Host "Zastavuji $($running -join ', ')…" -ForegroundColor Cyan
    Invoke-Compose stop @running
}
& docker compose @files run --rm --no-deps -T -v "$($item.FullName):${inside}:ro" api @restoreArgs
$code = $LASTEXITCODE

if ($code -eq 2) {
    throw 'Obnova nedokončena: PG je obnovená, data/ jen zčásti. Služby zůstávají zastavené — spusť obnovu znovu s -Overwrite.'
}
if ($code -ne 0) {
    if ($running.Count) {
        Write-Host "Cíl zůstal beze změny, startuji $($running -join ', ')…" -ForegroundColor Yellow
        Invoke-Compose start @running
    }
    throw "Obnova selhala (kód $code) — důvod je ve výpisu výše."
}

Write-Host 'Startuji stack…' -ForegroundColor Cyan
Invoke-Compose up -d
$address = (& docker compose @files port api 8000 | Select-Object -First 1)
if ($LASTEXITCODE -ne 0 -or -not $address) { throw 'Port API nejde zjistit (docker compose port api 8000).' }
$deadline = (Get-Date).AddSeconds(90)
while ($true) {
    try {
        $status = Invoke-RestMethod -Uri "http://$address/status" -TimeoutSec 5
        Write-Host ("/status odpovídá: engine {0}, poslední tick {1}" -f $status.engine, $status.last_tick_ts) -ForegroundColor Green
        break
    } catch {
        if ((Get-Date) -gt $deadline) { throw "/status na $address neodpověděl do 90 s: $($_.Exception.Message)" }
        Start-Sleep -Seconds 3
    }
}
