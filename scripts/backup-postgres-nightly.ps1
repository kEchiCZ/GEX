# Noční záloha PostgreSQL (#1566) — úloha „GEXLens backup-postgres" v Task Scheduleru
# (registrace `scripts/register-backup-postgres-task.ps1`).
#
# Data v PG nejdou znovu pořídit (věčný OI archiv R4, setupy s výsledky, verdikty,
# deník, signály). Bez pravidelné zálohy by poškození VHDX Docker Desktopu vzalo
# všechno od posledního ručního dumpu. Dump, kontrolu (PGDMP + pg_restore --list)
# a rotaci dělá backup-postgres.ps1; tady je jen plán, souběh a upozornění.
#
# Souběh: kompaktace VHDX a aktualizace WSL / Docker Desktopu Docker zastavují
# a drží zámek údržby — záloha počká, až ho nikdo nedrží. Sama ho nebere: ty úlohy
# čekají na proces „backup-postgres" (Get-BusyJobs) a zámek by při zaseknutém
# `docker exec` umlčel hlídač Dockeru. pg_dump čte konzistentní snímek a sběr
# nezastaví, brána trhu proto není; čas v pauze Globexu jen šetří notebook.
# Selhání = Send-OpsAlert (Telegram podle Nastavení → Notifikace) a kód 1, nikdy ticho;
# přeskočení (aplikace vypnutá, Docker neodpovídá) jen do logu, dokud poslední dump
# není starší než -MaxAgeHours.
#
# Ručně:  pwsh -NoProfile -File scripts/backup-postgres-nightly.ps1
[CmdletBinding()]
param(
    [string]$Target = 'D:\Programy\GEX\zalohy-pg',
    [int]$Keep = 14,
    [string]$Container = 'gex-postgres-1',
    # Jak dlouho čekat na doběhnutí kompaktace, aktualizace nebo deploye
    [int]$BusyWaitMinutes = 45,
    # Přeskočený běh se ohlásí, až je poslední dump starší — druhá noc za sebou
    [int]$MaxAgeHours = 36
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\DockerHost.ps1')
. (Join-Path $PSScriptRoot 'lib\OpsAlert.ps1')

function Get-LastDump {
    Get-ChildItem $Target -Filter 'gexlens-*.dump' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
}

# Poslední dump do textu upozornění — z něj je vidět, kolik dat je v ohrožení
function Get-LastDumpText {
    $last = Get-LastDump
    if (-not $last) { return "v $Target žádný dump" }
    'poslední dump {0} ({1:N0} h starý)' -f $last.Name, ((Get-Date) - $last.LastWriteTime).TotalHours
}

# Přeskočení se nehlásí hned (PC po probuzení, Docker ještě nejede; výpadek Dockeru
# hlásí hlídač), stará záloha ano: bez dumpu za $MaxAgeHours je v ohrožení víc dat
function Exit-Skipped([string]$Reason) {
    $last = Get-LastDump
    if (-not $last -or ((Get-Date) - $last.LastWriteTime).TotalHours -gt $MaxAgeHours) {
        Send-OpsAlert "Záloha PG neproběhla ($Reason); $(Get-LastDumpText)." -Topic 'maintenance'
        exit 1
    }
    Write-Step "$Reason — záloha PG se přeskakuje; $(Get-LastDumpText)."
    exit 0
}

$deadline = (Get-Date).AddMinutes($BusyWaitMinutes)
while ($true) {
    $busy = @(Get-BusyJobs 'deploy-engine-offhours') + @(Get-MaintenanceLockHolder | Where-Object { $_ })
    if ($busy.Count -eq 0) { break }
    if ((Get-Date) -gt $deadline) {
        Send-OpsAlert "Noční záloha PG neproběhla: ani po $BusyWaitMinutes min nedoběhlo $($busy -join ', '); $(Get-LastDumpText)." -Topic 'maintenance'
        exit 1
    }
    Write-Step "Běží $($busy -join ', ') — záloha PG čeká..."
    Start-Sleep -Seconds 30
}

# Aplikace vypnutá = od posledního běhu se v PG nic nezměnilo (jako walk-forward)
$ps = Invoke-Docker @('ps', '--filter', "name=$Container", '--filter', 'status=running', '--format', '{{.Names}}')
if ($ps.ExitCode -ne 0) { Exit-Skipped "Docker neodpovídá: $("$($ps.Err)".Trim())" }
if (-not "$($ps.Out)".Trim()) { Exit-Skipped "$Container neběží" }

Write-Step "Noční záloha PG do $Target (rotace $Keep)"
try {
    & (Join-Path $PSScriptRoot 'backup-postgres.ps1') -Target $Target -Keep $Keep -Container $Container
} catch {
    Send-OpsAlert "Noční záloha PG selhala: $($_.Exception.Message); $(Get-LastDumpText)." -Topic 'maintenance'
    exit 1
}
Write-Step "Noční záloha PG hotová; $(Get-LastDumpText)."
