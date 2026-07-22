/** Agregace 1m denních dat do timeframe košů (2m…1d) — čisté funkce v paměti.

Zdrojová 1m data jsou v kumulativní sémantice (OI/volume per buňka = stav v čase),
takže hodnota koše = poslední minuta koše; přírůstkové řady (Vol, OptVol) se sčítají,
cena se skládá do OHLC. Timeframe se tedy přepíná bez dalšího fetch.
*/
import type { DayData, LiveOverlay } from './useDayData'
import type { HeatmapGrid } from '../heatmap/grid'
import type { LevelLine, OverlayData, PriceBar } from '../heatmap/overlays'

/** Poslední minuta koše (ořezaná na konec dne). */
function bucketEnd(bucketIdx: number, bucketMinutes: number, minutes: number): number {
  return Math.min(minutes - 1, (bucketIdx + 1) * bucketMinutes - 1)
}

function aggregateLayer(
  layer: Float32Array | undefined,
  minutes: number,
  strikeCount: number,
  bucketMinutes: number,
  buckets: number,
): Float32Array | undefined {
  if (!layer) return undefined
  const result = new Float32Array(buckets * strikeCount)
  for (let strikeIdx = 0; strikeIdx < strikeCount; strikeIdx += 1) {
    for (let bucketIdx = 0; bucketIdx < buckets; bucketIdx += 1) {
      const source = strikeIdx * minutes + bucketEnd(bucketIdx, bucketMinutes, minutes)
      result[strikeIdx * buckets + bucketIdx] = layer[source]
    }
  }
  return result
}

/** Přírůstková řada (Vol, OptVol): součet koše. */
function sumSeries(values: number[], bucketMinutes: number, buckets: number): number[] {
  const result = Array.from({ length: buckets }, () => 0)
  values.forEach((value, index) => {
    result[Math.min(buckets - 1, Math.floor(index / bucketMinutes))] += value
  })
  return result
}

/** Kumulativní řada (CumΔ): poslední hodnota koše. */
function lastSeries(values: number[], bucketMinutes: number, buckets: number): number[] {
  return Array.from({ length: buckets }, (_, bucketIdx) => {
    const end = bucketEnd(bucketIdx, bucketMinutes, values.length)
    return values[end] ?? 0
  })
}

/** Řada s dírami (levels, spot): poslední ne-null hodnota koše. */
function lastNonNull(
  values: (number | null)[],
  bucketMinutes: number,
  buckets: number,
): (number | null)[] {
  return Array.from({ length: buckets }, (_, bucketIdx) => {
    const start = bucketIdx * bucketMinutes
    const end = bucketEnd(bucketIdx, bucketMinutes, values.length)
    for (let index = end; index >= start; index -= 1) {
      const value = values[index]
      if (value !== null && value !== undefined) return value
    }
    return null
  })
}

/** Skládání 1m barů do OHLC svíček koše (exportováno kvůli testům). */
export function aggregateBars(bars: PriceBar[], bucketMinutes: number): PriceBar[] {
  const byBucket = new Map<number, PriceBar[]>()
  for (const bar of bars) {
    const bucketIdx = Math.floor(bar.minuteIdx / bucketMinutes)
    const group = byBucket.get(bucketIdx)
    if (group) group.push(bar)
    else byBucket.set(bucketIdx, [bar])
  }
  const result: PriceBar[] = []
  let previousClose = Number.NaN
  for (const bucketIdx of [...byBucket.keys()].sort((a, b) => a - b)) {
    const group = byBucket.get(bucketIdx)!.sort((a, b) => a.minuteIdx - b.minuteIdx)
    const first = group[0]
    const last = group[group.length - 1]
    const open = first.open ?? first.close
    const close = last.close
    result.push({
      minuteIdx: bucketIdx,
      open,
      close,
      high: Math.max(...group.map((bar) => bar.high ?? bar.close)),
      low: Math.min(...group.map((bar) => bar.low ?? bar.close)),
      up: Number.isNaN(previousClose) ? close >= open : !(close < previousClose),
    })
    previousClose = close
  }
  return result
}

/** Živá vrstva (#141) do timeframe košů: rozdělaná svíčka splyne s košem, do kterého
její minuta patří — včetně už uzavřených minut téhož koše (`staticBars`). Volající
pak musí statickou svíčku toho koše vynechat, jinak by se kreslila dvakrát.

`gridMinutes` je počet minut PŘED agregací (kvůli mapování popisků náběžné hrany). */
export function aggregateLive(
  live: LiveOverlay,
  bucketMinutes: number,
  gridMinutes: number,
  staticBars: PriceBar[],
): LiveOverlay {
  if (bucketMinutes <= 1 || live.bars.length === 0) return live
  const buckets = Math.max(1, Math.ceil(gridMinutes / bucketMinutes))
  const staticByBucket = new Map(staticBars.map((bar) => [bar.minuteIdx, bar]))
  const byBucket = new Map<number, PriceBar[]>()
  for (const bar of live.bars) {
    const bucketIdx = Math.floor(bar.minuteIdx / bucketMinutes)
    const group = byBucket.get(bucketIdx)
    if (group) group.push(bar)
    else byBucket.set(bucketIdx, [bar])
  }
  const bars: PriceBar[] = []
  const labels: string[] = []
  for (const bucketIdx of [...byBucket.keys()].sort((a, b) => a - b)) {
    const group = byBucket.get(bucketIdx)!.sort((a, b) => a.minuteIdx - b.minuteIdx)
    const base = staticByBucket.get(bucketIdx) // uzavřené minuty téhož koše
    const first = group[0]
    const last = group[group.length - 1]
    const open = base?.open ?? base?.close ?? first.open ?? first.close
    const close = last.close
    const highs = group.map((bar) => bar.high ?? bar.close)
    const lows = group.map((bar) => bar.low ?? bar.close)
    if (base) {
      highs.push(base.high ?? base.close)
      lows.push(base.low ?? base.close)
    }
    // Směr vůči close předchozího koše (živého, jinak statického) — stejná
    // sémantika jako aggregateBars, ať koš po uzavření nemění barvu (#159)
    const previousClose = bars.at(-1)?.close ?? staticByBucket.get(bucketIdx - 1)?.close
    bars.push({
      minuteIdx: bucketIdx,
      open,
      close,
      high: Math.max(...highs),
      low: Math.min(...lows),
      up: previousClose === undefined ? close >= open : !(close < previousClose),
    })
    // Popisek potřebují jen koše za koncem gridu (náběžná hrana)
    if (bucketIdx >= buckets) {
      labels[bucketIdx - buckets] = live.labels[first.minuteIdx - gridMinutes] ?? ''
    }
  }
  return { bars, labels }
}

function aggregateOverlays(
  overlays: OverlayData,
  bucketMinutes: number,
  buckets: number,
): OverlayData {
  const line = (item: LevelLine): LevelLine => ({
    ...item,
    series: lastNonNull(item.series, bucketMinutes, buckets),
  })
  return {
    ...overlays,
    price: overlays.price ? aggregateBars(overlays.price, bucketMinutes) : undefined,
    levels: overlays.levels?.map(line),
    walls: overlays.walls?.map(line),
    sessions: overlays.sessions?.map((session) => ({
      ...session,
      minuteIdx: Math.floor(session.minuteIdx / bucketMinutes),
    })),
  }
}

/** Celý den agregovaný do timeframe košů; bucketMinutes ≤ 1 vrací originál. */
export function aggregateDay(day: DayData, bucketMinutes: number): DayData {
  if (bucketMinutes <= 1) return day
  const { minutes, strikes } = day.grid
  const strikeCount = strikes.length
  const buckets = Math.max(1, Math.ceil(minutes / bucketMinutes))

  const grid: HeatmapGrid = {
    minutes: buckets,
    strikes,
    layers: {
      call: aggregateLayer(day.grid.layers.call, minutes, strikeCount, bucketMinutes, buckets),
      put: aggregateLayer(day.grid.layers.put, minutes, strikeCount, bucketMinutes, buckets),
      signed: aggregateLayer(day.grid.layers.signed, minutes, strikeCount, bucketMinutes, buckets),
    },
    staleAge: day.grid.staleAge
      ? (aggregateLayer(day.grid.staleAge, minutes, strikeCount, bucketMinutes, buckets) ?? null)
      : null,
  }

  // Koš přebírá profil své poslední minuty — jen přemapování indexu, bez materializace (#142)
  const source = day.profileByMinute
  const profileByMinute = source
    ? {
        length: buckets,
        rowsAt: (bucketIdx: number) => source.rowsAt(bucketEnd(bucketIdx, bucketMinutes, minutes)),
      }
    : null

  return {
    source: day.source,
    grid,
    raw: day.raw, // surová 1m matice se nese dál (módy se aplikují před agregací)
    overlays: aggregateOverlays(day.overlays, bucketMinutes, buckets),
    panels: {
      vol: sumSeries(day.panels.vol, bucketMinutes, buckets),
      optVolCall: sumSeries(day.panels.optVolCall, bucketMinutes, buckets),
      optVolPut: sumSeries(day.panels.optVolPut, bucketMinutes, buckets),
      cumDelta: lastSeries(day.panels.cumDelta, bucketMinutes, buckets),
      deltaFlowCall: sumSeries(day.panels.deltaFlowCall, bucketMinutes, buckets),
      deltaFlowPut: sumSeries(day.panels.deltaFlowPut, bucketMinutes, buckets),
    },
    profileByMinute,
    demoProfileRows: day.demoProfileRows,
    // Koš přebírá Dyn GEX profil poslední minuty s daty (ADR-0009)
    gexProfile: day.gexProfile
      ? Array.from({ length: buckets }, (_, bucketIdx) => {
          const end = bucketEnd(bucketIdx, bucketMinutes, minutes)
          for (let minuteIdx = end; minuteIdx >= bucketIdx * bucketMinutes; minuteIdx -= 1) {
            const row = day.gexProfile![minuteIdx]
            if (row) return row
          }
          return null
        })
      : null,
    spotSeries: lastNonNull(day.spotSeries, bucketMinutes, buckets),
    minuteLabels: Array.from(
      { length: buckets },
      (_, bucketIdx) => day.minuteLabels[bucketIdx * bucketMinutes] ?? '',
    ),
    lastMinuteIso: day.lastMinuteIso,
  }
}
