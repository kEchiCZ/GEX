# Sdílená brána „je trh zavřený?" pro provozní skripty (deploy, úklid Dockeru,
# kompaktace VHDX). Globex jede neděle 17:00 CT → pátek 16:00 CT s denní pauzou
# 16:00–17:00 CT; restart Dockeru mimo tato okna udělá díru ve sběru, kterou
# nejde dohnat (#221). Jedna definice místo tří kopií (#1127).
#
# Použití:  . (Join-Path $PSScriptRoot 'lib\GlobexClock.ps1'); $clock = Get-GlobexClock
#           if (-not $clock.Closed) { throw "Trh je otevřený (CT $($clock.Label))" }

function Get-GlobexClock {
    # Id zóny: Windows zná 'Central Standard Time', Linux (pwsh bez ICU konverze)
    # jen IANA 'America/Chicago' — zkusí se obojí (#1094)
    $tz = $null
    foreach ($id in @('America/Chicago', 'Central Standard Time')) {
        try { $tz = [System.TimeZoneInfo]::FindSystemTimeZoneById($id); break } catch { }
    }
    if (-not $tz) { throw 'Časová zóna Chicago není v systému (zkoušeno America/Chicago, Central Standard Time) — bez ní nejde určit pauzu Globexu.' }
    $ct = [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $tz)
    $closed = $ct.DayOfWeek -eq 'Saturday' `
        -or ($ct.DayOfWeek -eq 'Sunday' -and $ct.Hour -lt 17) `
        -or ($ct.DayOfWeek -eq 'Friday' -and $ct.Hour -ge 16) `
        -or ($ct.Hour -eq 16)
    [pscustomobject]@{ Closed = [bool]$closed; Central = $ct; Label = $ct.ToString('ddd HH:mm') }
}
