/** Daily pohled: sloupec heatmapy = jeden uložený den (SPEC 7.1 Intraday/Daily).

Skládá se z denních /replay balíků — každý den přispívá stavem poslední minuty
(OI/volume jsou kumulativní) a denní OHLC svíčkou. Historie roste s retencí
snapshotů (14 dní, R4).
*/
import type { DayData } from './useDayData'
import { profileSourceOf } from './loader'
import type { ReplayDay } from './loader'
import type { HeatmapGrid } from '../heatmap/grid'
import type { LevelLine, PriceBar } from '../heatmap/overlays'
import type { ProfileRow } from '../profile/bars'

/** Popisek dne na ose X: 2026-07-16 → 16.7. */
export function dayLabel(date: string): string {
  const [, month, day] = date.split('-').map(Number)
  return Number.isFinite(month) && Number.isFinite(day) ? `${day}.${month}.` : date
}

function lastNonNull(series: (number | null)[] | undefined): number | null {
  if (!series) return null
  for (let index = series.length - 1; index >= 0; index -= 1) {
    if (series[index] !== null) return series[index]
  }
  return null
}

/** Denní OHLC svíčka z 1m barů dne. */
function dailyBar(dayIdx: number, bars: PriceBar[], previousClose: number): PriceBar | null {
  if (bars.length === 0) return null
  const sorted = [...bars].sort((a, b) => a.minuteIdx - b.minuteIdx)
  const open = sorted[0].open ?? sorted[0].close
  const close = sorted[sorted.length - 1].close
  return {
    minuteIdx: dayIdx,
    open,
    close,
    high: Math.max(...sorted.map((bar) => bar.high ?? bar.close)),
    low: Math.min(...sorted.map((bar) => bar.low ?? bar.close)),
    up: Number.isNaN(previousClose) ? close >= open : !(close < previousClose),
  }
}

/** Složení Daily datasetu z denních replay balíků (seřazených vzestupně dle data). */
export function buildDailyDay(days: ReplayDay[]): DayData {
  const columns = days.length
  const strikes = [...new Set(days.flatMap((day) => day.grid.strikes))].sort((a, b) => a - b)
  const strikeIndex = new Map(strikes.map((strike, index) => [strike, index]))
  const size = columns * strikes.length

  const call = new Float32Array(size)
  const put = new Float32Array(size)
  const vol = Array.from({ length: columns }, () => 0)
  const optVolCall = Array.from({ length: columns }, () => 0)
  const optVolPut = Array.from({ length: columns }, () => 0)
  const cumDelta = Array.from({ length: columns }, () => 0)
  const deltaFlowCall = Array.from({ length: columns }, () => 0)
  const deltaFlowPut = Array.from({ length: columns }, () => 0)
  const price: PriceBar[] = []
  const spotSeries: (number | null)[] = Array.from({ length: columns }, () => null)
  const profileByMinute: ProfileRow[][] = []
  const lineSeries = new Map<string, { color: string; series: (number | null)[] }>()

  let previousClose = Number.NaN
  days.forEach((day, dayIdx) => {
    const lastMinute = day.grid.minutes - 1
    day.grid.strikes.forEach((strike, sourceIdx) => {
      const targetIdx = strikeIndex.get(strike)!
      const source = sourceIdx * day.grid.minutes + lastMinute
      const target = targetIdx * columns + dayIdx
      call[target] = day.grid.layers.call?.[source] ?? 0
      put[target] = day.grid.layers.put?.[source] ?? 0
    })

    vol[dayIdx] = day.panels.vol.reduce((sum, value) => sum + value, 0)
    optVolCall[dayIdx] = day.panels.optVolCall.reduce((sum, value) => sum + value, 0)
    optVolPut[dayIdx] = day.panels.optVolPut.reduce((sum, value) => sum + value, 0)
    cumDelta[dayIdx] = day.panels.cumDelta.at(-1) ?? 0
    deltaFlowCall[dayIdx] = day.panels.deltaFlowCall.reduce((sum, value) => sum + value, 0)
    deltaFlowPut[dayIdx] = day.panels.deltaFlowPut.reduce((sum, value) => sum + value, 0)

    const bar = dailyBar(dayIdx, day.overlays.price ?? [], previousClose)
    if (bar) {
      price.push(bar)
      previousClose = bar.close
      spotSeries[dayIdx] = bar.close
    }
    profileByMinute.push(day.profileByMinute.rowsAt(day.profileByMinute.length - 1))

    for (const line of [...(day.overlays.levels ?? []), ...(day.overlays.walls ?? [])]) {
      if (!lineSeries.has(line.name)) {
        lineSeries.set(line.name, {
          color: line.color,
          series: Array.from({ length: columns }, () => null),
        })
      }
      lineSeries.get(line.name)!.series[dayIdx] = lastNonNull(line.series)
    }
  })

  const toLines = (names: string[]): LevelLine[] =>
    names.filter((name) => lineSeries.has(name)).map((name) => ({ name, ...lineSeries.get(name)! }))

  const grid: HeatmapGrid = {
    minutes: columns,
    strikes,
    layers: { call, put },
    staleAge: null,
  }

  return {
    source: 'replay',
    grid,
    raw: null, // Daily skládá dny — módy/škály se přepínají jen intraday
    overlays: {
      price,
      levels: toLines(['flip', 'centroid']),
      walls: toLines(['call_wall', 'put_wall']),
      sessions: [],
      timestamp: days.at(-1)?.date ?? '',
    },
    panels: { vol, optVolCall, optVolPut, cumDelta, deltaFlowCall, deltaFlowPut },
    profileByMinute: profileSourceOf(profileByMinute),
    demoProfileRows: null,
    spotSeries,
    minuteLabels: days.map((day) => dayLabel(day.date)),
    lastMinuteIso: null, // Daily pohled se neprojektuje (sloupec = den)
    gexProfile: null, // Dyn GEX profil je intraday vrstva (ADR-0009)
  }
}
