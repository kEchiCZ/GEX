# Záloha PostgreSQL GEXLens do zvolené složky (#439).
#
# Používá pg_dump PŘÍMO z Postgres kontejneru, takže nevyžaduje přestavěný
# API image ani klienta na hostiteli a verze klienta vždy sedí se serverem.
#
# Použití:
#   .\scripts\backup-postgres.ps1                      # do %USERPROFILE%\GEXLens-backup
#   .\scripts\backup-postgres.ps1 -Target D:\Zalohy    # do vlastní složky
#   .\scripts\backup-postgres.ps1 -Keep 10             # nechat 10 nejnovějších
#
# Cíl volit MIMO adresář projektu — záloha uvnitř repa chrání jen před smazáním
# Docker volume, ne před selháním disku, a do gitu nepatří (obsahuje všechna data).
[CmdletBinding()]
param(
    [string]$Target = (Join-Path $env:USERPROFILE 'GEXLens-backup'),
    [int]$Keep = 14,
    [string]$Container = 'gex-postgres-1',
    [string]$Database = 'gexlens',
    [string]$User = 'gexlens'
)
$ErrorActionPreference = 'Stop'

if (-not (docker ps --filter "name=$Container" --format '{{.Names}}')) {
    throw "Kontejner $Container neběží — spusť stack (docker compose up -d) a zkus znovu."
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$resolved = [IO.Path]::GetFullPath($Target)
if ($resolved.StartsWith($repoRoot, [StringComparison]::OrdinalIgnoreCase)) {
    Write-Warning "Cíl je uvnitř repozitáře ($repoRoot). Záloha na tomtéž disku nechrání před jeho selháním a do gitu nepatří."
}

New-Item -ItemType Directory -Force -Path $resolved | Out-Null
$stamp = Get-Date -Format 'yyyy-MM-dd_HHmm'
$file = Join-Path $resolved "gexlens-$stamp.dump"

Write-Host "Zálohuji $Database z $Container -> $file" -ForegroundColor Cyan
# --format=custom: komprimované, obnovitelné pg_restore i selektivně.
# Přesměrování dělá cmd, ne PowerShell: ten by binární stdout převedl na text
# a dump by byl poškozený (Set-Content -Encoding Byte na to nestačí).
$dump = "docker exec $Container pg_dump --username $User --format=custom --no-owner --no-privileges $Database"
cmd /c "$dump > `"$file`""
if ($LASTEXITCODE -ne 0) { Remove-Item $file -ErrorAction SilentlyContinue; throw "pg_dump selhal (kód $LASTEXITCODE)" }
# Platný custom dump začíná signaturou PGDMP — chytí i tiché poškození roury.
# Čte se přes FileStream: `Get-Content -Encoding Byte` v PowerShellu 7 neexistuje
# (nahrazeno `-AsByteStream`) a skript na něm padal AŽ PO vytvoření dumpu, takže
# záloha vznikla, ale ohlásila se jako chyba a neproběhla rotace.
$head = New-Object byte[] 5
$stream = [IO.File]::OpenRead($file)
try { [void]$stream.Read($head, 0, $head.Length) } finally { $stream.Dispose() }
$magic = [Text.Encoding]::ASCII.GetString($head)
if ($magic -ne 'PGDMP') { Remove-Item $file -Force; throw "Záloha není platný pg_dump (hlavička '$magic')" }

$size = (Get-Item $file).Length / 1MB
Write-Host ("Hotovo: {0:N1} MB" -f $size) -ForegroundColor Green

# Rotace — starší zálohy nad limit smazat, ať složka neroste donekonečna
$old = Get-ChildItem $resolved -Filter 'gexlens-*.dump' | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep
if ($old) {
    $old | Remove-Item -Force
    Write-Host "Rotace: smazáno $($old.Count) starších záloh (limit $Keep)"
}

Write-Host "Obnova:  docker exec -i $Container pg_restore --username $User --dbname $Database --clean --if-exists < `"$file`"" -ForegroundColor DarkGray
