# Sdílená obálka nad `docker` pro start skripty.
#
# Proč existuje: `start-prod.ps1` i `start-dev.ps1` volaly docker a při nenulovém
# návratovém kódu vyhodily vlastní hlášku „docker compose selhal — běží Docker
# Desktop?". Skutečný výstup dockeru se přitom ztratil, takže hláška tvrdila
# příčinu, kterou nikdo neověřil, a pravá příčina se musela hledat ručním
# zopakováním příkazu (1. 9. 2026: build prošel hned napodruhé, takže šlo
# o přechodný stav — z hlášky to ale poznat nešlo).
#
# `Tee-Object` drží obojí: výstup teče živě (u buildu jsou to minuty progresu,
# které nechceš zadržet do konce) a zároveň se ukládá, aby šel přiložit k chybě.

Set-StrictMode -Version Latest

function Assert-DockerReady {
    <#
    .SYNOPSIS
    Ověří, že docker vůbec jde použít — a rozliší DVA různé důvody, proč ne.

    Stará hláška „docker compose selhal — běží Docker Desktop?" hádala příčinu
    u každého selhání. Chybějící příkaz a nespuštěný démon jsou přitom různé
    problémy s různým řešením, a většina selhání není ani jedno z toho.
    #>
    [CmdletBinding()]
    param()

    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Příkaz `docker` není v PATH — je nainstalovaný Docker Desktop?'
    }
    & docker info --format '{{.ServerVersion}}' *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker démon neodpovídá — spusť Docker Desktop a zkus znovu.'
    }
}

function Invoke-DockerChecked {
    <#
    .SYNOPSIS
    Spustí `docker` s danými argumenty; při nenulovém exit kódu vyhodí chybu
    i se skutečným výstupem dockeru.

    .PARAMETER Arguments
    Argumenty pro docker, např. @('compose','up','-d','--build').

    .PARAMETER FailureHint
    Věta popisující, CO se nepovedlo (ne proč) — důvod dodá výstup dockeru.

    .PARAMETER TailLines
    Kolik posledních řádků výstupu přiložit k chybě.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$FailureHint,
        [int]$TailLines = 25
    )

    # 2>&1 sloučí stderr do proudu — docker píše progres i chyby tam
    & docker @Arguments 2>&1 | Tee-Object -Variable dockerOutput
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        $tail = @($dockerOutput) |
            Select-Object -Last $TailLines |
            ForEach-Object { "  $_" }
        $detail = if ($tail) {
            ($tail -join [Environment]::NewLine)
        } else {
            '  (docker nevypsal nic)'
        }
        throw "$FailureHint`nDocker skončil s kódem $exitCode. Poslední řádky výstupu:`n$detail"
    }
}
