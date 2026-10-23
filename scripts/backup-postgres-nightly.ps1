# Noční záloha PostgreSQL (#1566) — úloha „GEXLens backup-postgres" v Task Scheduleru
# (registrace `scripts/register-backup-postgres-task.ps1`).
#
# Data v PG nejdou znovu pořídit (věčný OI archiv R4, setupy s výsledky, verdikty,
# deník, signály). Bez pravidelné zálohy by poškození VHDX Docker Desktopu vzalo
# všechno od posledního ručního dumpu. Dump, kontrolu (hlavička PGDMP a celé čtení
# pg_restore) a rotaci dělá backup-postgres.ps1; tady je jen plán, souběh a upozornění.
#
# Souběh: kompaktace VHDX a aktualizace WSL / Docker Desktopu Docker zastavují
# a drží zámek údržby — záloha počká, až ho nikdo nedrží, stejně jako na deploy
# a ruční zálohu. Sama zámek nebere: ty úlohy čekají na proces „backup-postgres"
# (Get-BusyJobs) a zámek by při zaseknutém `docker exec` umlčel hlídač Dockeru.
# Záloha běží jako podproces s limitem (Invoke-Native, #1279): zaseknutý dump
# skončí upozorněním, ne tichým zabitím úlohy Task Schedulerem.
# pg_dump čte konzistentní snímek a sběr nezastaví, brána trhu proto není; čas
# v pauze Globexu jen šetří notebook.
# Selhání dumpu = Send-OpsAlert (Telegram podle Nastavení → Notifikace) a kód 1.
# Přeskočení (aplikace vypnutá, Docker neodpovídá, údržba trvá) jen do logu, dokud
# poslední dump není starší než -MaxAgeHours (Test-PgBackupStale).
#
# Ručně:  pwsh -NoProfile -File scripts/backup-postgres-nightly.ps1
[CmdletBinding()]
param(
    [string]$Target = 'D:\Programy\GEX\zalohy-pg',
    [int]$Keep = 14,
    [string]$Container = 'gex-postgres-1',
    # Rozpočet úlohy 60 min: čekání na údržbu + dump s kontrolou (obvykle ~2 min)
    [int]$BusyWaitMinutes = 30,
    [int]$DumpTimeoutMinutes = 20,
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
# hlásí hlídač; update-offhours mohl právě zálohovat), stará záloha ano
function Exit-Skipped([string]$Reason) {
    $last = Get-LastDump
    if (Test-PgBackupStale $(if ($last) { $last.LastWriteTime }) $MaxAgeHours) {
        Send-OpsAlert "Záloha PG neproběhla ($Reason); $(Get-LastDumpText)." -Topic 'maintenance'
        exit 1
    }
    Write-Step "$Reason — záloha PG se přeskakuje; $(Get-LastDumpText)."
    exit 0
}

# Na ruční zálohu se čeká taky: ve stejné minutě by obě psaly do téhož souboru
$deadline = (Get-Date).AddMinutes($BusyWaitMinutes)
while ($true) {
    $busy = @(Get-BusyJobs 'deploy-engine-offhours|backup-postgres\.ps1') + @(Get-MaintenanceLockHolder | Where-Object { $_ })
    if ($busy.Count -eq 0) { break }
    if ((Get-Date) -gt $deadline) { Exit-Skipped "ani po $BusyWaitMinutes min nedoběhlo $($busy -join ', ')" }
    Write-Step "Běží $($busy -join ', ') — záloha PG čeká..."
    Start-Sleep -Seconds 30
}

# Aplikace vypnutá = od posledního běhu se v PG nic nezměnilo (jako walk-forward)
$ps = Invoke-Docker @('ps', '--filter', "name=$Container", '--filter', 'status=running', '--format', '{{.Names}}')
if ($ps.ExitCode -ne 0) { Exit-Skipped "Docker neodpovídá: $("$($ps.Err)".Trim())" }
if (-not "$($ps.Out)".Trim()) { Exit-Skipped "$Container neběží" }

Write-Step "Noční záloha PG do $Target (rotace $Keep, limit $DumpTimeoutMinutes min)"
$pwsh = (Get-Process -Id $PID).Path
$backup = Join-Path $PSScriptRoot 'backup-postgres.ps1'
$result = Invoke-Native $pwsh @('-NoProfile', '-NonInteractive', '-File', $backup, '-Target', $Target,
    '-Keep', "$Keep", '-Container', $Container) ($DumpTimeoutMinutes * 60)
if ("$($result.Out)".Trim()) { Write-Host "$($result.Out)".TrimEnd() }
if ($result.ExitCode -ne 0) {
    $why = if ($result.ExitCode -eq -1) { "nedoběhla do $DumpTimeoutMinutes min" } else { "kód $($result.ExitCode)" }
    $err = ("$($result.Err)".Trim() -split "`r?`n" | Where-Object { $_ } | Select-Object -Last 3) -join ' '
    Send-OpsAlert "Noční záloha PG selhala ($why): $err; $(Get-LastDumpText)." -Topic 'maintenance'
    exit 1
}
Write-Step "Noční záloha PG hotová; $(Get-LastDumpText)."
