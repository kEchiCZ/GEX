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
    [switch]$Force,
    # Linux server (#1094): přidá compose.server.yml (IB Gateway jako kontejner)
    [switch]$Server
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# Sada compose souborů (#1094): na PC jen compose.yml, na serveru navíc override
# s IB Gateway — bez něj by `up -d engine` vrátil engine na host.docker.internal
$composeArgs = @('-f', 'compose.yml')
if ($Server) { $composeArgs += @('-f', 'compose.server.yml') }

function Write-Step($text) { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $text" }

# Log běžícího kontejneru zmizí s jeho recreate (json-file driver žije s
# kontejnerem). 7. 9. (#1054) tak po deployi nešlo dohledat, co engine dělal
# v pátek, a falešný poplach se řešil hodinu bez důkazu. Proto se před KAŽDÝM
# recreate log uloží do data/logs/ — a když se uložit nedá, deploy se zastaví:
# log je důkaz, ne volitelný krok (#1056).
function Save-EngineLog([string]$Suffix = '') {
    $dir = Join-Path $repo 'data/logs'
    New-Item -ItemType Directory -Force $dir | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmm'
    $path = Join-Path $dir "engine-$stamp$Suffix.log"
    docker logs gex-engine-1 2>&1 | Out-File -FilePath $path -Encoding utf8
    if ($LASTEXITCODE -ne 0) {
        throw "Uložení logu enginu do $path selhalo (docker logs exit $LASTEXITCODE) — bez důkazu se nenasazuje (#1056)."
    }
    $size = (Get-Item $path).Length
    if ($size -eq 0) {
        throw "Log enginu je prázdný ($path) — kontejner gex-engine-1 neběží nebo nic nezapsal; ověř ručně, než ho nahradíš (#1056)."
    }
    Write-Step "Log enginu uložen: $path ($([math]::Round($size / 1KB)) kB)"
    # Retence 30 dní — data/logs/ je mimo retention job enginu (ten sahá jen
    # na snapshots/ a derived/), takže úklid dělá skript sám
    Get-ChildItem $dir -Filter 'engine-*.log' |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
        Remove-Item -Force
}

# ── 1) Je trh zavřený? ────────────────────────────────────────────────
# Globex jede neděle 17:00 CT → pátek 16:00 CT s denní pauzou 16:00–17:00 CT.
# Id zóny: Windows zná 'Central Standard Time', Linux (pwsh bez ICU konverze)
# jen IANA 'America/Chicago' — zkusí se obojí, jinak by deploy na serveru spadl
# dřív, než cokoli zkontroluje (#1094)
$tz = $null
foreach ($id in @('America/Chicago', 'Central Standard Time')) {
    try { $tz = [System.TimeZoneInfo]::FindSystemTimeZoneById($id); break } catch { }
}
if (-not $tz) { throw "Časová zóna Chicago není v systému (zkoušeno America/Chicago, Central Standard Time) — bez ní nejde určit pauzu Globexu." }
$ct = [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $tz)
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
docker compose @composeArgs build engine
if ($LASTEXITCODE -ne 0) { throw 'Build enginu selhal — nic se nerestartovalo.' }

# ── 3a) Log dosavadního běhu, než ho recreate smaže (#1056) ───────────
Save-EngineLog

# ── 4) Restart jen enginu ─────────────────────────────────────────────
# --no-deps: API, Postgres ani news-engine se nedotýkáme
docker compose @composeArgs up -d --no-deps engine
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

    # ── 5b) API + frontend ve stejném okně (#513, 26. 8.) ─────────────
    # Buildy image na prod daemonu umí daemon shodit (výpadek 26. 8. při
    # buildu během US RTH) — proto se i api/frontend staví až v pauze
    # Globexu, kdy případný pád nic nesbíraného nestojí. Selhání téhle
    # části NEshazuje engine deploy: engine už je zdravý, exit 0 výše
    # se jen posune za tento blok a chyba se ohlásí warningem.
    foreach ($svc in @('api', 'frontend')) {
        docker compose @composeArgs build $svc
        if ($LASTEXITCODE -ne 0) { Write-Warning "Build $svc selhal — služba zůstává na staré verzi."; continue }
        docker compose @composeArgs up -d --no-deps $svc
        if ($LASTEXITCODE -ne 0) { Write-Warning "Start $svc selhal — zkontroluj docker logs." }
        else { Write-Step "OK — $svc nasazen." }
    }
    exit 0
}

# ── 6) Rollback ───────────────────────────────────────────────────────
Write-Warning "Engine není zdravý (status $state, restartů $restarts, pád v logu: $crashed) — vracím $BackupTag."
# Log padlé verze je přesně to, co se bude zítra ladit — uložit, než ho
# force-recreate zahodí (#1056); selhání uložení tady rollback NEzastaví
try { Save-EngineLog -Suffix '-crashed' } catch { Write-Warning "Log padlé verze se nepodařilo uložit: $_" }
docker tag "gex-engine:$BackupTag" gex-engine:latest
docker compose @composeArgs up -d --no-deps --force-recreate engine
Start-Sleep -Seconds 20
$after = (docker inspect -f '{{.State.Status}}' gex-engine-1).Trim()
Write-Step "Po rollbacku: $after"
throw 'Nasazení enginu selhalo, vrácena předchozí verze. Zkontroluj docker logs gex-engine-1.'
