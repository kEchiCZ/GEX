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
    [switch]$Server,
    # Lokální build místo stažení image z GHCR (#1139) — jen nouzově (CI nefunguje)
    [switch]$Build,
    # Jak dlouho čekat, než CI dodá image pro HEAD main (s)
    [int]$ImageWaitSeconds = 900
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\OpsAlert.ps1')
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# Sada compose souborů (#1094): na PC jen compose.yml, na serveru navíc override
# s IB Gateway — bez něj by `up -d engine` vrátil engine na host.docker.internal
$composeArgs = @('-f', 'compose.yml')
if ($Server) { $composeArgs += @('-f', 'compose.server.yml') }

function Write-Step($text) { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $text" }

# Image se staví v CI (#1139) — doma se jen stahují. `latest` v GHCR může
# být ještě z předchozího mergu (workflow běží ~5 min), proto se po pullu
# porovná label revize image s HEAD main a čeká se, dokud nesedí.
$script:Images = @{
    engine   = 'ghcr.io/kechicz/gex-python:latest'
    api      = 'ghcr.io/kechicz/gex-python:latest'
    frontend = 'ghcr.io/kechicz/gex-frontend:latest'
}
function Get-ImageRevision([string]$Image) {
    $rev = docker image inspect $Image --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($rev | Out-String).Trim()
}
function Update-ServiceImage([string]$Service, [string]$Head) {
    <# Stáhne image služby z GHCR a počká, až jeho revize odpovídá HEAD main.
       Vrací $true při shodě; $false po vypršení (volající rozhodne). #>
    if ($Build) {
        docker compose @composeArgs build $Service
        return ($LASTEXITCODE -eq 0)
    }
    $deadline = (Get-Date).AddSeconds($ImageWaitSeconds)
    while ($true) {
        docker compose @composeArgs pull --quiet $Service
        if ($LASTEXITCODE -ne 0) { Write-Warning "Pull $Service selhal (GHCR nedostupné? balíček neveřejný?)"; return $false }
        $rev = Get-ImageRevision $script:Images[$Service]
        if ($rev -and $Head.StartsWith($rev)) { Write-Step "Image $Service = revize $($rev.Substring(0, 7)) (HEAD main)."; return $true }
        if ((Get-Date) -gt $deadline) {
            Write-Warning "Image $Service má revizi '$rev', HEAD main je $($Head.Substring(0, 7)) — CI ještě nedoběhlo nebo selhalo (workflow Images)."
            return $false
        }
        Write-Step "Image $Service zatím z revize '$rev' — čekám na CI (do $($deadline.ToString('HH:mm:ss')))..."
        Start-Sleep -Seconds 30
    }
}

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
# Sdílená brána (scripts/lib/GlobexClock.ps1): Globex jede neděle 17:00 CT →
# pátek 16:00 CT s denní pauzou 16:00–17:00 CT.
. (Join-Path $PSScriptRoot 'lib\GlobexClock.ps1')
$clock = Get-GlobexClock
if (-not $clock.Closed -and -not $Force) {
    throw "Trh je otevřený (CT $($clock.Label)) — restart by udělal díru ve sběru. Použij -Force jen vědomě."
}
Write-Step "Trh zavřený (CT $($clock.Label)) — pokračuju."

# ── 2) Kód z main ─────────────────────────────────────────────────────
$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne 'main') { throw "Nasazuje se výhradně z main (jsi na '$branch')." }
git pull --ff-only
$head = (git rev-parse HEAD).Trim()
# Image z CI nese revizi POSLEDNÍHO commitu, který sahal na vstupy workflow
# Images (stejný seznam cest jako `paths:` v .github/workflows/images.yml).
# Commit jen ve scripts/ nebo docs/ image nestaví, takže porovnání s HEAD
# by po něm navždy hlásilo „CI nedoběhlo" (24. 9. 2026: 8315200 vs. 4bef411).
$imageHead = (git log -1 --format=%H -- engine api news-engine pyproject.toml uv.lock frontend docker .github/workflows/images.yml).Trim()
Write-Step "main na $($head.Substring(0, 7)); image se čeká z revize $($imageHead.Substring(0, 7))"

# ── 3) Záloha běžícího image + stažení nového z GHCR (#1139) ──────────
# Rollback tag na lokálním jménu `gex-python:pre-*` (docker-cleanup.ps1 nechá 3)
docker tag $script:Images.engine "gex-python:$BackupTag"
Write-Step "Záloha image: gex-python:$BackupTag"
if (-not (Update-ServiceImage 'engine' $imageHead)) { throw 'Image enginu pro HEAD main není k dispozici — nic se nerestartovalo. (Nouzově: -Build.)' }

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
        if (-not (Update-ServiceImage $svc $imageHead)) { Write-Warning "Image $svc pro HEAD main není k dispozici — služba zůstává na staré verzi."; continue }
        docker compose @composeArgs up -d --no-deps $svc
        if ($LASTEXITCODE -ne 0) { Write-Warning "Start $svc selhal — zkontroluj docker logs." }
        else { Write-Step "OK — $svc nasazen." }
    }

    # ── 5c) Úklid Dockeru (#1127) ──────────────────────────────────────
    # Každý deploy přidá rollback tag a vrstvy do build cache; 12. 9. 2026
    # to narostlo na 71 GB, zaplnilo disk a shodilo containerd. Úklid maže
    # jen rollback tagy (nechá 3), build cache > 7 dnů a osiřelé image —
    # volumes nikdy. Selhání úklidu deploy NEshazuje (engine už je zdravý),
    # nedostatek místa hlásí skript sám alertem do zvonku.
    try {
        & (Join-Path $PSScriptRoot 'docker-cleanup.ps1') -KeepTags 3 -CacheAgeHours 168
        if ($LASTEXITCODE -eq 2) { Write-Warning 'Úklid Dockeru: málo místa — viz alert disk_low a scripts/compact-docker-vhdx.ps1.' }
    } catch { Write-Warning "Úklid Dockeru selhal: $_" }
    exit 0
}

# ── 6) Rollback ───────────────────────────────────────────────────────
Write-Warning "Engine není zdravý (status $state, restartů $restarts, pád v logu: $crashed) — vracím $BackupTag."
# Log padlé verze je přesně to, co se bude zítra ladit — uložit, než ho
# force-recreate zahodí (#1056); selhání uložení tady rollback NEzastaví
try { Save-EngineLog -Suffix '-crashed' } catch { Write-Warning "Log padlé verze se nepodařilo uložit: $_" }
docker tag "gex-python:$BackupTag" $script:Images.engine
docker compose @composeArgs up -d --no-deps --force-recreate engine
Start-Sleep -Seconds 20
$after = (docker inspect -f '{{.State.Status}}' gex-engine-1).Trim()
Write-Step "Po rollbacku: $after"
# Deploy běží i bez dohledu (23:xx) — upozornit mimo Docker (#1279). Rollback
# restartuje jen engine (--no-deps), API běží: nastavení Telegramu (#1284) si
# Send-OpsAlert načte samo; když API neodpoví, pošle se (fail-open)
Send-OpsAlert "Deploy enginu selhal, vrácena verze $BackupTag (stav po rollbacku: $after). Zkontroluj docker logs gex-engine-1." -Topic 'maintenance'
throw 'Nasazení enginu selhalo, vrácena předchozí verze. Zkontroluj docker logs gex-engine-1.'
