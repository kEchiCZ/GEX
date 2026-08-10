# Naplnění DEV prostředí kopií produkčních dat (#568).
#
# 1. PG: obnoví nejnovější zálohu (výstup scripts/backup-postgres.ps1)
#    do dev Postgresu (projekt gexdev, volume gexdev_pgdata).
# 2. Parquet: zrcadlí data/ → data-dev/ (robocopy /MIR — data-dev je
#    jednorázová kopie, smí se přepsat; OPAČNÝM směrem nikdy).
#
# Produkce může při seedu běžet — čte se záloha a data/ se jen kopíruje.
#
# Použití:
#   .\scripts\seed-dev.ps1                            # nejnovější záloha
#   .\scripts\seed-dev.ps1 -Backup C:\...\gexlens-2026-08-10_0700.dump
[CmdletBinding()]
param(
    [string]$Backup,
    [string]$BackupDir = (Join-Path $env:USERPROFILE 'GEXLens-backup'),
    [string]$Container = 'gexdev-postgres-1',
    [string]$Database = 'gexlens',
    [string]$User = 'gexlens'
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not $Backup) {
    $newest = Get-ChildItem $BackupDir -Filter 'gexlens-*.dump' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $newest) {
        throw "V $BackupDir není žádná záloha — vytvoř ji: .\scripts\backup-postgres.ps1 (vyžaduje běžící produkci)."
    }
    $Backup = $newest.FullName
}
if (-not (Test-Path $Backup)) { throw "Záloha $Backup neexistuje." }

# Dev postgres musí běžet — nastartovat umíme sami, na zbytek stacku nesaháme
if (-not (docker ps --filter "name=$Container" --format '{{.Names}}')) {
    Write-Host 'Startuji dev postgres…' -ForegroundColor Cyan
    docker compose -f compose.dev.yml up -d --wait postgres
    if ($LASTEXITCODE -ne 0) { throw 'Dev postgres nenaběhl.' }
}

Write-Host "Obnovuji $([IO.Path]::GetFileName($Backup)) -> $Container/$Database" -ForegroundColor Cyan
# Přesměrování dělá cmd, ne PowerShell — ten by binární vstup rozbil převodem
# na text (stejný důvod jako v backup-postgres.ps1). --clean --if-exists:
# opakovaný seed přepíše předchozí obsah.
cmd /c "docker exec -i $Container pg_restore --username $User --dbname $Database --clean --if-exists --no-owner --no-privileges < `"$Backup`""
if ($LASTEXITCODE -ne 0) { throw "pg_restore selhal (kód $LASTEXITCODE)" }
Write-Host 'PG obnoveno.' -ForegroundColor Green

Write-Host 'Kopíruji data/ -> data-dev/ …' -ForegroundColor Cyan
robocopy 'data' 'data-dev' /MIR /NFL /NDL /NJH /NJS | Out-Null
# Robocopy: 0–7 = úspěch (0 beze změn, 1 zkopírováno, …), >=8 = chyba
if ($LASTEXITCODE -ge 8) { throw "robocopy selhal (kód $LASTEXITCODE)" }
$size = (Get-ChildItem 'data-dev' -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("Hotovo: data-dev {0:N0} MB. DEV má čerstvou kopii produkce." -f $size) -ForegroundColor Green
