# Noční walk-forward report parametrů setupů (#794 fáze 3, ADR-0034).
#
# Jen čte archiv a PG (Max Pain z OI archivu, baseline z parameter store) a
# zapíše markdown do data/reports/walkforward-<datum>.md. NIC nezapisuje do DB —
# autonomie stupeň 1: případný návrh verze parametrů schvaluje a odesílá člověk.
#
# Spouštět po settle (např. 23:30) ručně nebo z Task Scheduleru (registrace:
# `scripts/register-walkforward-task.ps1`, úloha „GEXLens walk-forward"):
#   pwsh -NoProfile -File scripts/walkforward-nightly.ps1
# Připojení k DB: GEXLENS_HOST_DATABASE_URL, jinak se složí z GEXLENS_PG_PASSWORD
# (prostředí nebo .env) a portu 55432; hodnotu nikdy nevypisuje.
[CmdletBinding()]
param(
    [string]$Symbols = 'ES,NQ',
    [int]$InSample = 20,
    [int]$OutSample = 5,
    [ValidateSet('sharpe', 'sum_r')][string]$Metric = 'sharpe',
    # Kolik dní reportů držet (adresář je mimo retention job enginu)
    [int]$KeepDays = 90
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

function Write-Step($text) { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $text" }

# Běží aplikace? Bez PG kontejneru není z čeho číst (Max Pain, baseline ze store)
# — Task Scheduler pouští úlohu i po probuzení PC, kdy compose ještě nejede.
# Tichý konec s hláškou, ne chyba: „nic k měření" není porucha.
$pg = docker ps --filter 'name=gex-postgres-1' --filter 'status=running' --format '{{.Names}}' 2>$null
if (-not $pg) {
    Write-Step 'gex-postgres-1 neběží (aplikace vypnutá) — walk-forward se přeskakuje.'
    exit 0
}

# Připojení z HOSTITELE: compose publikuje PG na 55432 (na vývojovém PC běží na 5432
# nativní PostgreSQL — kap. 4 ADMIN manuálu), takže GEXLENS_DATABASE_URL z .env
# (adresa v compose síti) tady neplatí. URL se skládá z GEXLENS_PG_PASSWORD a
# hodnota se nikdy nevypisuje (CLAUDE.md pravidlo 5). Přebít lze proměnnou
# GEXLENS_HOST_DATABASE_URL.
$dbUrl = $env:GEXLENS_HOST_DATABASE_URL
if (-not $dbUrl) {
    $password = $env:GEXLENS_PG_PASSWORD
    if (-not $password) {
        $line = Get-Content .env -ErrorAction SilentlyContinue |
            Where-Object { $_ -match '^GEXLENS_PG_PASSWORD=' } | Select-Object -First 1
        if (-not $line) { throw 'GEXLENS_PG_PASSWORD není v prostředí ani v .env' }
        $password = $line.Substring('GEXLENS_PG_PASSWORD='.Length).Trim('"')
    }
    $dbUrl = "postgresql+psycopg://gexlens:$([uri]::EscapeDataString($password))@127.0.0.1:55432/gexlens"
    Write-Step 'DB URL složena z GEXLENS_PG_PASSWORD (port 55432)'
}

$dir = Join-Path $repo 'data/reports'
New-Item -ItemType Directory -Force $dir | Out-Null
$out = Join-Path $dir "walkforward-$(Get-Date -Format 'yyyy-MM-dd').md"

Write-Step "Walk-forward $Symbols (IS $InSample / OOS $OutSample, $Metric) -> $out"
uv run python scripts/walkforward_setups.py --db $dbUrl --symbols $Symbols --in-sample $InSample `
    --out-sample $OutSample --metric $Metric --out $out
if ($LASTEXITCODE -ne 0) { throw "walkforward_setups.py skončil s kódem $LASTEXITCODE" }

# Verdikt do konzole (řádky souhrnné tabulky), ať je vidět bez otevírání souboru
Get-Content $out | Select-String -Pattern '^\| (ES|NQ|portfolio)' | ForEach-Object { $_.Line }

Get-ChildItem $dir -Filter 'walkforward-*.md' |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
    Remove-Item -Force
Write-Step 'Hotovo.'
