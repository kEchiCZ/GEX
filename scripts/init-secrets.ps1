# Vygeneruje chybějící tajemství do lokálního .env (#542).
#
# Klíče se do repa nikdy nepíšou — .env je v .gitignore. Skript je idempotentní:
# existující hodnoty nepřepisuje, jen doplní, co chybí.
#
# GEXLENS_PG_PASSWORD  — heslo k PostgreSQL (compose ho vyžaduje, jinak nenastartuje)
# GEXLENS_API_TOKEN    — sdílené tajemství pro /internal/* a /backup (engine ↔ API)
#
# POZOR: na JIŽ BĚŽÍCÍ databázi změna GEXLENS_PG_PASSWORD sama nestačí —
# POSTGRES_PASSWORD platí jen při prvním initdb. Skript proto heslo rovnou
# přepíše i v běžícím serveru (ALTER USER), pokud kontejner jede.

param(
    [string]$EnvPath = (Join-Path $PSScriptRoot '..\.env')
)

$ErrorActionPreference = 'Stop'

function New-Secret {
    # 32 náhodných bajtů → URL-safe base64 (bez znaků, které rozbíjejí DSN)
    $bytes = [byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

if (-not (Test-Path $EnvPath)) {
    New-Item -ItemType File -Path $EnvPath | Out-Null
    Write-Host "Vytvořen prázdný $EnvPath"
}

$lines = @(Get-Content -LiteralPath $EnvPath)
$added = @()
$pgPassword = $null

foreach ($key in @('GEXLENS_PG_PASSWORD', 'GEXLENS_API_TOKEN')) {
    $existing = $lines | Where-Object { $_ -match "^\s*$key\s*=\s*\S" }
    if ($existing) {
        Write-Host "$key už v .env je — ponechávám"
        if ($key -eq 'GEXLENS_PG_PASSWORD') {
            $pgPassword = ($existing[0] -replace "^\s*$key\s*=\s*", '').Trim()
        }
        continue
    }
    $secret = New-Secret
    $lines += "$key=$secret"
    $added += $key
    if ($key -eq 'GEXLENS_PG_PASSWORD') { $pgPassword = $secret }
}

if ($added.Count -gt 0) {
    Set-Content -LiteralPath $EnvPath -Value $lines -Encoding utf8
    Write-Host "Doplněno do .env: $($added -join ', ')"
} else {
    Write-Host 'Nic doplňovat netřeba.'
}

# Běžící DB: heslo z initdb se novou proměnnou nezmění, musí se přepsat v serveru
$container = (docker compose ps -q postgres 2>$null)
if ($LASTEXITCODE -eq 0 -and $container -and $pgPassword) {
    $escaped = $pgPassword.Replace("'", "''")
    docker exec -e PGPASSWORD_NEW=$escaped $container `
        psql -U gexlens -d gexlens -v ON_ERROR_STOP=1 `
        -c "ALTER USER gexlens PASSWORD '$escaped';" | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host 'Heslo přepsáno i v běžícím PostgreSQL (ALTER USER).'
        Write-Host 'Restartuj stack, ať služby vezmou nové DSN: docker compose up -d'
    } else {
        Write-Warning 'ALTER USER selhal — heslo v .env nesedí s běžící DB. Sladit ručně.'
    }
} else {
    Write-Host 'PostgreSQL kontejner neběží — heslo se použije při dalším startu.'
}
