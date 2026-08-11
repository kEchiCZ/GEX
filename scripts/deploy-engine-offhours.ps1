# Nasazení enginu mimo seanci (#600) — build, restart, kontrola, rollback při selhání.
#
# Engine se restartuje jen tehdy, když je trh zavřený: restart za běhu udělá
# v opčních snapshotech díru, kterou nejde dohnat (#221 backfill neexistuje).
# Bezpečné okno je denní pauza Globexu 16:00–17:00 CT (= 23:00–24:00 v CEST).
#
# Skript je psaný na běh BEZ dohledu: když engine po restartu nenaběhne, vrátí
# se předchozí image, protože stojící sběr přes noc je horší než nenasazená změna.
[CmdletBinding()]
param(
    # Značka zálohy předchozího image — návrat je pak `docker tag`, ne rebuild
    [string]$BackupTag = 'pre-600',
    # Jak dlouho po startu se čeká, než se kontroluje stav (s)
    [int]$SettleSeconds = 90,
    # Přeskočí kontrolu obchodních hodin (jen pro ruční nasazení mimo okno)
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

function Write-Step($text) { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $text" }

# ── 1) Je trh zavřený? ────────────────────────────────────────────────
# Globex jede neděle 17:00 CT → pátek 16:00 CT s denní pauzou 16:00–17:00 CT.
$ct = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'Central Standard Time')
$closed = $ct.DayOfWeek -eq 'Saturday' `
    -or ($ct.DayOfWeek -eq 'Sunday' -and $ct.Hour -lt 17) `
    -or ($ct.DayOfWeek -eq 'Friday' -and $ct.Hour -ge 16) `
    -or ($ct.Hour -eq 16)
if (-not $closed -and -not $Force) {
    throw "Trh je otevřený (CT $($ct.ToString('ddd HH:mm'))) — restart by udělal díru ve sběru. Použij -Force jen vědomě."
}
Write-Step "Trh zavřený (CT $($ct.ToString('ddd HH:mm'))) — pokračuju."

# ── 2) Kód z main ─────────────────────────────────────────────────────
$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne 'main') { throw "Nasazuje se výhradně z main (jsi na '$branch')." }
git pull --ff-only
Write-Step "main na $((git rev-parse --short HEAD).Trim())"

# ── 3) Záloha běžícího image + build ──────────────────────────────────
docker tag gex-engine:latest "gex-engine:$BackupTag"
Write-Step "Záloha image: gex-engine:$BackupTag"
docker compose -f compose.yml build engine
if ($LASTEXITCODE -ne 0) { throw 'Build enginu selhal — nic se nerestartovalo.' }

# ── 4) Restart jen enginu ─────────────────────────────────────────────
# --no-deps: API, Postgres ani news-engine se nedotýkáme
docker compose -f compose.yml up -d --no-deps engine
if ($LASTEXITCODE -ne 0) { throw 'Start enginu selhal.' }
Write-Step "Engine nastartován, čekám ${SettleSeconds} s na ustálení."
Start-Sleep -Seconds $SettleSeconds

# ── 5) Kontrola: běží a nespadl na výjimce ────────────────────────────
$state = (docker inspect -f '{{.State.Status}}' gex-engine-1).Trim()
$restarts = [int](docker inspect -f '{{.RestartCount}}' gex-engine-1).Trim()
$log = docker logs gex-engine-1 --tail 200 2>&1 | Out-String
$crashed = $log -match 'Traceback \(most recent call last\)|CRITICAL'
$healthy = ($state -eq 'running') -and ($restarts -eq 0) -and (-not $crashed)

if ($healthy) {
    Write-Step "OK — engine běží (status $state, restartů $restarts, log čistý)."
    exit 0
}

# ── 6) Rollback ───────────────────────────────────────────────────────
Write-Warning "Engine není zdravý (status $state, restartů $restarts, pád v logu: $crashed) — vracím $BackupTag."
docker tag "gex-engine:$BackupTag" gex-engine:latest
docker compose -f compose.yml up -d --no-deps --force-recreate engine
Start-Sleep -Seconds 20
$after = (docker inspect -f '{{.State.Status}}' gex-engine-1).Trim()
Write-Step "Po rollbacku: $after"
throw 'Nasazení enginu selhalo, vrácena předchozí verze. Zkontroluj docker logs gex-engine-1.'
