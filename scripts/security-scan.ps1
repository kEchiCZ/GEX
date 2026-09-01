# Opakovatelná bezpečnostní prověrka (#542) — jedním příkazem.
#
#   pwsh scripts/security-scan.ps1            # rychlé skeny (secrets + závislosti)
#   pwsh scripts/security-scan.ps1 -Images    # navíc trivy nad postavenými images
#
# gitleaks a trivy se pouštějí přes Docker, aby nebylo co instalovat; pip-audit
# přes `uvx`. Nálezy se vypisují do konzole, JSON reporty do -OutDir.
#
# Návratový kód: 1, pokud něco našel (hodí se pro CI), jinak 0.

param(
    [switch]$Images,
    [string]$OutDir = (Join-Path ([System.IO.Path]::GetTempPath()) 'gexlens-security')
)

$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$failures = @()

function Write-Section($title) {
    Write-Host ''
    Write-Host "── $title " -NoNewline
    Write-Host ('─' * [Math]::Max(0, 60 - $title.Length))
}

# ── 1. Secrets v celé git historii ─────────────────────────────────────────
Write-Section 'gitleaks (git historie)'
$leaksReport = Join-Path $OutDir 'gitleaks.json'
docker run --rm -v "${repo}:/repo" -v "${OutDir}:/out" zricethezav/gitleaks:latest `
    detect --source /repo --report-format json --report-path /out/gitleaks.json --exit-code 0 2>&1 |
    Select-Object -Last 3
if (Test-Path $leaksReport) {
    $leaks = @(Get-Content $leaksReport -Raw | ConvertFrom-Json)
    if ($leaks.Count -gt 0) {
        $failures += "gitleaks: $($leaks.Count) nálezů"
        $leaks | ForEach-Object { Write-Host "  ! $($_.RuleID) — $($_.File) @ $($_.Commit.Substring(0,8))" }
    } else {
        Write-Host '  OK — žádný uniklý klíč'
    }
}

# ── 2. Python závislosti ───────────────────────────────────────────────────
Write-Section 'pip-audit (engine, api, news-engine)'
$requirements = Join-Path $OutDir 'requirements.txt'
# --all-packages: bez něj se vyexportují jen dev závislosti rootu, ne workspace
uv export --format requirements-txt --all-packages --no-emit-workspace -o $requirements --quiet
$pipReport = Join-Path $OutDir 'pip-audit.json'
uvx pip-audit -r $requirements --no-deps -f json -o $pipReport 2>&1 | Select-Object -Last 2
if (Test-Path $pipReport) {
    $audit = Get-Content $pipReport -Raw | ConvertFrom-Json
    $vulnerable = @($audit.dependencies | Where-Object { $_.vulns.Count -gt 0 })
    if ($vulnerable.Count -gt 0) {
        $failures += "pip-audit: $($vulnerable.Count) zranitelných balíčků"
        $vulnerable | ForEach-Object {
            foreach ($v in $_.vulns) { Write-Host "  ! $($_.name) $($_.version) — $($v.id)" }
        }
    } else {
        Write-Host "  OK — $($audit.dependencies.Count) balíčků bez známé zranitelnosti"
    }
}

# ── 3. Frontend závislosti ─────────────────────────────────────────────────
Write-Section 'npm audit (frontend)'
$npmReport = Join-Path $OutDir 'npm-audit.json'
Push-Location (Join-Path $repo 'frontend')
cmd /c "npm audit --json > `"$npmReport`" 2>&1" | Out-Null
Pop-Location
if (Test-Path $npmReport) {
    $npm = Get-Content $npmReport -Raw | ConvertFrom-Json
    $counts = $npm.metadata.vulnerabilities
    if ($counts.critical -gt 0 -or $counts.high -gt 0) {
        $failures += "npm audit: $($counts.critical) critical, $($counts.high) high"
    }
    Write-Host "  critical $($counts.critical) · high $($counts.high) · moderate $($counts.moderate) · low $($counts.low)"
    foreach ($name in $npm.vulnerabilities.PSObject.Properties.Name) {
        $v = $npm.vulnerabilities.$name
        if ($v.severity -in @('critical', 'high')) {
            Write-Host "  ! $name ($($v.severity), $(if ($v.isDirect) { 'přímá' } else { 'tranzitivní' }))"
        }
    }
}

# ── 4. Docker images (volitelně — trvá minuty) ─────────────────────────────
if ($Images) {
    Write-Section 'trivy (docker images)'
    foreach ($image in @('gex-api:latest', 'gex-frontend:latest', 'postgres:16')) {
        $safe = $image.Replace(':', '-').Replace('/', '-')
        $report = Join-Path $OutDir "trivy-$safe.json"
        docker run --rm -v //var/run/docker.sock:/var/run/docker.sock -v "${OutDir}:/out" `
            aquasec/trivy:latest image --severity HIGH,CRITICAL --quiet `
            --format json --output "/out/trivy-$safe.json" $image 2>&1 | Select-Object -Last 2
        if (Test-Path $report) {
            $trivy = Get-Content $report -Raw | ConvertFrom-Json
            $crit = 0; $high = 0
            foreach ($r in $trivy.Results) {
                foreach ($v in $r.Vulnerabilities) {
                    if ($v.Severity -eq 'CRITICAL') { $crit++ } elseif ($v.Severity -eq 'HIGH') { $high++ }
                }
            }
            Write-Host "  ${image}: CRITICAL $crit · HIGH $high"
            if ($crit -gt 0) { $failures += "trivy ${image}: $crit critical" }
        }
    }
    Write-Host '  Pozn.: nálezy jsou zpravidla OS balíky base image — řeší je rebuild s čerstvou bází.'
}

# ── 5. tastytrade: výhradně read scope, nikdy heslo (#620, ADR-0025) ───────
# Statická pojistka na politiku přístupu. gitleaks hlídá UNIKLÉ tajemství,
# tohle hlídá ZPŮSOB přístupu: scope `trade` nebo přihlášení heslem přes
# `/sessions` dá tokenu právo obchodovat na živém účtu, i kdyby nikdy neunikl.
Write-Section 'tastytrade přístup (#620)'
$srcDirs = @('engine/src', 'api/src', 'news-engine/src', 'scripts') |
    ForEach-Object { Join-Path $repo $_ } | Where-Object { Test-Path $_ }
$pySrc = @(Get-ChildItem -Path $srcDirs -Recurse -Include *.py -File -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '__pycache__' })

# `/sessions` jako REÁLNÁ cesta požadavku (v uvozovkách). Zmínky v komentářích
# a docstringu jsou psané v obrácených apostrofech, takže je tenhle vzor mine
# a zákaz smí zůstat zdokumentovaný v kódu.
$sessionHits = @($pySrc | Select-String -Pattern '["'']/sessions' -ErrorAction SilentlyContinue)
if ($sessionHits.Count -gt 0) {
    $failures += "tastytrade: přihlášení heslem přes /sessions ($($sessionHits.Count)x)"
    $sessionHits | ForEach-Object { Write-Host "  ! $($_.Path):$($_.LineNumber)" }
} else {
    Write-Host '  OK — nikde se nevolá /sessions (přihlášení heslem)'
}

# Scope `trade` nebo `write` v jakémkoli požadavku na token
$scopeHits = @($pySrc | Select-String -Pattern 'scope.{0,60}(trade|write)' -ErrorAction SilentlyContinue)
if ($scopeHits.Count -gt 0) {
    $failures += "tastytrade: požadován scope trade/write ($($scopeHits.Count)x)"
    $scopeHits | ForEach-Object { Write-Host "  ! $($_.Path):$($_.LineNumber)" }
} else {
    Write-Host '  OK — nikde se nežádá scope trade ani write'
}

# `.env.example` musí klíče POJMENOVAT a nechat PRÁZDNÉ — vyplněná hodnota
# v šabloně je únik, i když vypadá jako placeholder
$exampleFile = Join-Path $repo '.env.example'
if (Test-Path $exampleFile) {
    $exampleOk = $true
    foreach ($key in @('GEXLENS_TASTY_CLIENT_SECRET', 'GEXLENS_TASTY_REFRESH_TOKEN')) {
        $line = @(Select-String -Path $exampleFile -Pattern "^$key=" -ErrorAction SilentlyContinue)
        if ($line.Count -eq 0) {
            $failures += ".env.example: chybí $key"
            Write-Host "  ! .env.example neuvádí $key"
            $exampleOk = $false
        } elseif ($line[0].Line.Trim() -ne "$key=") {
            $failures += ".env.example: $key má vyplněnou hodnotu"
            Write-Host "  ! .env.example má u $key hodnotu — šablona musí zůstat prázdná"
            $exampleOk = $false
        }
    }
    if ($exampleOk) { Write-Host '  OK — šablona klíče uvádí a nechává prázdné' }
}

# Skutečný `.env` se nesmí dostat do gitu
$trackedEnv = @(git -C $repo ls-files --error-unmatch .env .env.dev 2>$null)
$global:LASTEXITCODE = 0  # `--error-unmatch` hlásí nenalezení kódem, což je tady OČEKÁVANÝ stav
if ($trackedEnv.Count -gt 0) {
    $failures += "git sleduje soubor s tajemstvími: $($trackedEnv -join ', ')"
    Write-Host "  ! git sleduje $($trackedEnv -join ', ')"
} else {
    Write-Host '  OK — .env ani .env.dev git nesleduje'
}

Write-Section 'Souhrn'
Write-Host "Reporty: $OutDir"
if ($failures.Count -gt 0) {
    $failures | ForEach-Object { Write-Host "  ! $_" }
    exit 1
}
Write-Host '  Bez nálezů.'
exit 0
