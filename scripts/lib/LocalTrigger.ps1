# Týdenní spouštěč Task Scheduleru v MÍSTNÍM čase (#1277, #1278).
#
# New-ScheduledTaskTrigger ukládá StartBoundary s offsetem (…T23:30:00+02:00),
# což je „synchronizovat napříč časovými pásmy": Windows pak drží čas v UTC a po
# konci letního času úloha běží o hodinu dřív (walk-forward 23:30 → 22:30 SEČ,
# tedy v otevřeném trhu; kompaktace 23:05 → 22:05). Bez offsetu se čas vztahuje
# k místnímu času a přechod DST přežije. Jedna definice pro všechny register-*.ps1.
#
# Použití:  . (Join-Path $PSScriptRoot 'lib\LocalTrigger.ps1')
#           $trigger = New-LocalWeeklyTrigger -DaysOfWeek Monday, Friday -At '23:05'
function New-LocalWeeklyTrigger {
    param(
        [Parameter(Mandatory)][System.DayOfWeek[]]$DaysOfWeek,
        [Parameter(Mandatory)][string]$At
    )
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DaysOfWeek -At $At
    $trigger.StartBoundary = ([datetime]$trigger.StartBoundary).ToString('s')
    $trigger
}
