# Aktualizace WSL a Docker Desktopu jen v pauze Globexu (#1552; rozhodnutí vlastníka 13. 10. 2026).
#
# 13. 10. 2026 nainstaloval Windows Update sám WSL 3.0.3.0 v 03:10 UTC (Globex),
# ukončil VM Dockeru a Docker Desktop engine neobnovil → 4 h 49 min bez dat.
# Vlastník: aktualizace automaticky, ale jen v pauze; ručně je dělat nechce.
# Úloha „GEXLens update-offhours" (register-update-offhours-task.ps1) spouští
# tento skript denně v pauze Globexu; skript sám hlídá, že je trh zavřený.
#
# Postup:
#   1) brána trhu (zavřeno a do otevření ≥ MinMinutesToOpen), čekání na deploy,
#      walk-forward a zálohu PG a zámek údržby (kompaktace VHDX, #1552);
#   2) WSL: `wsl --update`; změnila-li se verze → `docker desktop restart`,
#      kontrola kontejnerů gex-* a spojení enginu s IBKR (/status);
#   3) Docker Desktop: `docker desktop update --check-only`; je-li novější verze →
#      čerstvá záloha PG (backup-postgres.ps1) + `pg_restore --list` nad dumpem
#      (bez úspěšné zálohy se NEaktualizuje), přesné počty řádků všech tabulek,
#      `docker desktop update --quiet`, nová verze, kontejnery, engine, počty
#      řádků po (žádná tabulka nesmí zmizet ani ztratit polovinu řádků — instalátor
#      starších verzí uměl smazat datový disk WSL s volume pgdata, opraveno ve 4.95.0).
# Problém → upozornění mimo Docker (Telegram, přepínač „maintenance") a exit 1.
# Docker neodpovídá už před aktualizací → jen log (výpadek hlásí hlídač při
# otevřeném trhu); engine bez spojení s IBKR po restartu → upozornění jen do
# hodiny před otevřením (o víkendu je IB Gateway běžně odhlášená).
#
#   pwsh -File scripts/update-wsl-docker-offhours.ps1 -DryRun            jen rozhodnutí a dostupné verze
#   pwsh -File scripts/update-wsl-docker-offhours.ps1 -SkipWsl           jen Docker Desktop (ručně v pauze, jako správce)
#   pwsh -File scripts/update-wsl-docker-offhours.ps1 -SkipDockerDesktop jen WSL
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SkipWsl,
    [switch]$SkipDockerDesktop,
    # Rezerva do otevření Globexu: aktualizace Docker Desktopu = záloha PG (~1 min),
    # stažení a instalace (~5–10 min), start daemonu a enginu (≤ 10 min)
    [int]$MinMinutesToOpen = 30,
    [int]$BusyWaitMinutes = 20,
    # Cíl zálohy PG (shodný s výchozím backup-postgres.ps1)
    [string]$BackupTarget = 'D:\Programy\GEX\zalohy-pg'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\GlobexClock.ps1')
. (Join-Path $PSScriptRoot 'lib\OpsAlert.ps1')
. (Join-Path $PSScriptRoot 'lib\DockerHost.ps1')

function Test-Window([string]$What) {
    $clock = Get-GlobexClock
    if (-not $clock.Closed) { Write-Step "Trh je otevřený (CT $($clock.Label)) — $What odkládám na další pauzu."; return $false }
    if ($clock.MinutesToOpen -lt $MinMinutesToOpen) {
        Write-Step ("Do otevření Globexu zbývá {0:N0} min (< {1}) — {2} odkládám na další pauzu." -f $clock.MinutesToOpen, $MinMinutesToOpen, $What)
        return $false
    }
    Write-Step ("Trh zavřený (CT {0}, do otevření {1:N0} min) — {2}." -f $clock.Label, $clock.MinutesToOpen, $What)
    $true
}

# ── 1) Brána trhu a souběžné úlohy ───────────────────────────────────
# DryRun pokračuje i při otevřeném trhu — jen vypíše verze a rozhodnutí
if (-not (Test-Window 'aktualizace') -and -not $DryRun) { exit 0 }
# Kompaktaci VHDX hlídá zámek údržby: elevovaný proces jinak nejde poznat
$busyPattern = 'deploy-engine-offhours|walkforward-nightly|backup-postgres'
if ($DryRun) {
    $busy = @(Get-BusyJobs $busyPattern) + @(Get-MaintenanceLockHolder | Where-Object { $_ })
    if ($busy.Count -gt 0) { Write-Step "DryRun: běží $($busy -join ', ') — aktualizace by počkala." }
} elseif (-not (Wait-MaintenanceSlot 'update-wsl-docker-offhours' $busyPattern $BusyWaitMinutes)) {
    Write-Step 'Aktualizace odkládám na další pauzu.'
    exit 0
}
# Zámek se uvolní i při exit a výjimce (finally na konci skriptu)
try {
    if (-not $DryRun) {
        if (-not (Test-Window 'aktualizace')) { exit 0 }
        if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw 'Spusť jako správce (instalace MSI WSL a Docker Desktopu vyžaduje elevaci) — nebo přes úlohu „GEXLens update-offhours".'
        }
    }

    $expected = @(Get-GexRunning)
    if ($expected.Count -eq 0) {
        # Docker neběží: záloha PG nejde a restart i upozornění při otevřeném trhu patří
        # hlídači (docker-watchdog.ps1) — trh je teď zavřený, proto jen log
        Write-Step 'Docker neodpovídá nebo neběží žádný kontejner gex-* — nic neaktualizuji.'
        exit $(if ($DryRun) { 0 } else { 1 })
    }
    Write-Step "Běží $($expected.Count): $($expected -join ', ')"
    # Nastavení Telegramu teď, dokud API běží — během restartu by neodpovědělo (#1284)
    $alertPolicy = if ($DryRun) { $null } else { Get-OpsAlertPolicy }
    $problems = [System.Collections.Generic.List[string]]::new()

    # Po restartu Dockeru: daemon, kontejnery, engine; vrací popis problému nebo $null
    function Test-StackAfter([string]$What) {
        if (-not (Wait-DockerReady 10)) {
            Write-Warning "Docker po $What do 10 min nenaběhl — zkouším docker desktop start."
            [void](Invoke-Docker @('desktop', 'start', '--timeout', '300') 330)
            if (-not (Wait-DockerReady 5)) { return "Docker po $What nenaběhl — sběr dat stojí, zkontroluj Docker Desktop." }
        }
        $missing = @(Restore-GexRunning $expected)
        $engine = Wait-EngineConnected 5
        $engineText = if ($engine) { "engine $($engine.Engine), IBKR $($engine.Connection)" } else { 'API /status neodpovídá' }
        Write-Step "Po ${What}: kontejnery $(@(Get-GexRunning).Count)/$($expected.Count), $engineText"
        if ($missing.Count -gt 0) { return "Po $What neběží $($missing -join ', ')." }
        if (-not $engine -or $engine.Connection -ne 'connected') {
            $clock = Get-GlobexClock
            $text = "Po $What engine není připojený k IBKR ($engineText)."
            # Do hodiny před otevřením (pauza všedního dne, nedělní večer) = data chybět budou;
            # jinak (sobota) je IB Gateway běžně odhlášená — jen log
            if ($clock.MinutesToOpen -le 60) { return $text }
            Write-Warning "$text Do otevření $([math]::Round($clock.MinutesToOpen)) min — jen log."
        }
        $null
    }

    # ── 2) WSL ───────────────────────────────────────────────────────────
    if (-not $SkipWsl) {
        $wslBefore = Get-WslVersion
        if ($DryRun) {
            Write-Step "DryRun: WSL $wslBefore — teď by proběhlo wsl --update (dostupnost verze bez instalace nejde zjistit)."
        } else {
            $env:WSL_UTF8 = '1'  # wsl.exe jinak píše UTF-16 a log je nečitelný
            $result = Invoke-Native 'wsl.exe' @('--update') 900
            $text = "$($result.Out)$($result.Err)".Trim()
            if ($text) { Write-Host $text }
            $wslAfter = Get-WslVersion
            if ($result.ExitCode -ne 0) { $problems.Add("wsl --update skončil kódem $($result.ExitCode) (WSL $wslBefore → $wslAfter).") }
            if ($wslAfter -ne $wslBefore) {
                Write-Step "WSL $wslBefore → $wslAfter — restartuji Docker Desktop."
                $restart = Invoke-Docker @('desktop', 'restart', '--timeout', '300') 330
                if ($restart.ExitCode -ne 0) { Write-Warning "docker desktop restart skončil $($restart.ExitCode): $("$($restart.Err)".Trim())" }
                $problem = Test-StackAfter "aktualizaci WSL $wslBefore → $wslAfter"
                if ($problem) { $problems.Add($problem) }
            } else {
                Write-Step "WSL $wslBefore — beze změny."
            }
        }
    }

    # ── 3) Docker Desktop ────────────────────────────────────────────────
    function Update-DockerDesktop {
        $ddBefore = Get-DockerDesktopVersion
        $check = Invoke-Docker @('desktop', 'update', '--check-only') 120
        $checkText = "$($check.Out)$($check.Err)".Trim()
        if ($check.ExitCode -ne 0) { return "docker desktop update --check-only skončil $($check.ExitCode): $checkText" }
        if ($checkText -notmatch 'Docker Desktop (\d+\.\d+\.\d+) is available') {
            Write-Step "Docker Desktop $ddBefore — novější verze není ($(($checkText -split "`r?`n")[0]))."
            return $null
        }
        $available = $matches[1]
        if ($DryRun) { Write-Step "DryRun: Docker Desktop $ddBefore → $available by proběhl po záloze PG."; return $null }
        if (-not (Test-Window "aktualizaci Docker Desktop $ddBefore → $available")) { return $null }

        # Záloha PG — bez úspěšné a čitelné zálohy se neaktualizuje
        $started = Get-Date
        try { & (Join-Path $PSScriptRoot 'backup-postgres.ps1') -Target $BackupTarget }
        catch { return "Docker Desktop $ddBefore → $available neaktualizován: záloha PG selhala ($($_.Exception.Message))." }
        $dump = Get-ChildItem $BackupTarget -Filter 'gexlens-*.dump' | Where-Object { $_.LastWriteTime -ge $started.AddMinutes(-1) } |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $dump) { return "Docker Desktop $ddBefore → $available neaktualizován: čerstvý dump v $BackupTarget nenalezen." }
        # Čitelnost (pg_restore --list) ověřil backup-postgres.ps1 — při selhání by vyhodil výjimku výše (#1566)
        Write-Step ("Záloha PG {0} ({1:N0} MB) ověřena pg_restore --list." -f $dump.Name, ($dump.Length / 1MB))

        $countsBefore = Get-PgRowCounts
        if (-not $countsBefore) { return "Docker Desktop $ddBefore → $available neaktualizován: počty řádků PG nejde načíst." }
        $total = ($countsBefore.Values | Measure-Object -Sum).Sum
        Write-Step "Počty řádků PG před: $($countsBefore.Count) tabulek, $total řádků."

        Write-Step "docker desktop update --quiet ($ddBefore → $available)..."
        $update = Invoke-Docker @('desktop', 'update', '--quiet') 1800
        $updateText = "$($update.Out)$($update.Err)".Trim()
        if ($updateText) { Write-Host $updateText }
        if ($update.ExitCode -ne 0) { Write-Warning "docker desktop update skončil kódem $($update.ExitCode)." }
        # Instalátor může doběhnout až po návratu příkazu — čekat na novou verzi v registru
        $deadline = (Get-Date).AddMinutes(20)
        while ((Get-DockerDesktopVersion) -eq $ddBefore -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 15 }
        $ddAfter = Get-DockerDesktopVersion
        $issues = @()
        if ($ddAfter -eq $ddBefore) { $issues += "verze zůstala $ddBefore (update kód $($update.ExitCode))" }
        else { Write-Step "Docker Desktop $ddBefore → $ddAfter." }

        $problem = Test-StackAfter "aktualizaci Docker Desktop $ddBefore → $ddAfter"
        if ($problem) { $issues += $problem }
        $countsAfter = Get-PgRowCounts
        if (-not $countsAfter) { $issues += 'počty řádků PG po aktualizaci nejde načíst' }
        else {
            $lost = @(Compare-PgRowCounts $countsBefore $countsAfter)
            $decreased = @(Compare-PgRowCounts $countsBefore $countsAfter -MaxDropShare 0)
            $totalAfter = ($countsAfter.Values | Measure-Object -Sum).Sum
            Write-Step "Počty řádků PG po: $($countsAfter.Count) tabulek, $totalAfter řádků."
            if ($decreased.Count -gt 0) { Write-Step "Pokles řádků (přepočty jsou běžné): $($decreased -join ', ')" }
            if ($lost.Count -gt 0) { $issues += "v PG zmizely tabulky nebo polovina řádků: $($lost -join ', ') — zkontrolovat, záloha $($dump.Name)" }
        }
        if ($issues.Count -gt 0) { return "Aktualizace Docker Desktop: $($issues -join '; ')" }
        $null
    }
    if (-not $SkipDockerDesktop) {
        try {
            $problem = Update-DockerDesktop
            if ($problem) { $problems.Add($problem) }
        } catch {
            $problems.Add("Aktualizace Docker Desktop spadla: $($_.Exception.Message)")
        }
    }

    if ($problems.Count -gt 0) {
        Send-OpsAlert ("Aktualizace v pauze: " + ($problems -join ' ')) -Topic 'maintenance' -Policy $alertPolicy
        exit 1
    }
    Write-Step 'Hotovo.'
} finally {
    Exit-MaintenanceLock
}
