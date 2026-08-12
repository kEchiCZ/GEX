/** Agregace 1m denních dat do timeframe košů (2m…1d) — čisté funkce v paměti.

Zdrojová 1m data jsou v kumulativní sémantice (OI/volume per buňka = stav v čase),
takže hodnota koše = poslední minuta koše; přírůstkové řady (Vol, OptVol) se sčítají,
cena se skládá do OHLC. Timeframe se tedy přepíná bez dalšího fetch.

Hranice košů určuje wall-clock plán (`heatmap/buckets.ts`), ne index minuty na ose —
5m koš tedy vždy začíná na `HH:00/05/10…` jako v TradingView (#584).
*/
import { minuteLabel } from './useDayData'
import { bucketStartMs, cachedBucketPlan } from '../heatmap/buckets'
import type { DayData, LiveOverlay } from './useDayData'
import type { BucketPlan } from '../heatmap/buckets'
import type { HeatmapGrid } from '../heatmap/grid'
import type { LevelLine, OverlayData, PriceBar } from '../heatmap/overlays'

function aggregateLayer(
  layer: Float32Array | undefined,
  minutes: number,
  strikeCount: number,
  plan: BucketPlan,
): Float32Array | undefined {
  if (!layer) return undefined
  const { buckets, ends } = plan
  const result = new Float32Array(buckets * strikeCount)
  for (let strikeIdx = 0; strikeIdx < strikeCount; strikeIdx += 1) {
    for (let bucketIdx = 0; bucketIdx < buckets; bucketIdx += 1) {
      const source = strikeIdx * minutes + ends[bucketIdx]
      result[strikeIdx * buckets + bucketIdx] = layer[source]
    }
  }
  return result
}

/** Přírůstková řada (Vol, OptVol): součet koše. */
function sumSeries(values: number[], plan: BucketPlan): number[] {
  const result = Array.from({ length: plan.buckets }, () => 0)
  values.forEach((value, index) => {
    const bucketIdx = plan.bucketOf[index]
    if (bucketIdx !== undefined) result[bucketIdx] += value
  })
  return result
}

/** Kumulativní řada (CumΔ): poslední hodnota koše. */
function lastSeries(values: number[], plan: BucketPlan): number[] {
  return Array.from({ length: plan.buckets }, (_, bucketIdx) => values[plan.ends[bucketIdx]] ?? 0)
}

/** Řada s dírami (levels, spot): poslední ne-null hodnota koše. */
function lastNonNull<T>(values: (T | null)[], plan: BucketPlan): (T | null)[] {
  return Array.from({ length: plan.buckets }, (_, bucketIdx) => {
    for (let index = plan.ends[bucketIdx]; index >= plan.starts[bucketIdx]; index -= 1) {
      const value = values[index]
      if (value !== null && value !== undefined) return value
    }
    return null
  })
}

/** Skládání 1m barů do OHLC svíček koše (exportováno kvůli testům). */
export function aggregateBars(bars: PriceBar[], plan: BucketPlan): PriceBar[] {
  const byBucket = new Map<number, PriceBar[]>()
  for (const bar of bars) {
    const bucketIdx = plan.bucketOf[bar.minuteIdx]
    if (bucketIdx === undefined) continue // bar mimo osu (nemělo by nastat)
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

`gridMinutes` je počet minut PŘED agregací a `minutesIso` je jejich osa — koše
náběžné hrany se stejně jako naměřené zarovnávají na wall-clock (#584). */
export function aggregateLive(
  live: LiveOverlay,
  bucketMinutes: number,
  gridMinutes: number,
  staticBars: PriceBar[],
  minutesIso: string[],
): LiveOverlay {
  if (bucketMinutes <= 1 || live.bars.length === 0) return live
  const plan = cachedBucketPlan(minutesIso, gridMinutes, bucketMinutes)
  const buckets = plan.buckets
  const staticByBucket = new Map(staticBars.map((bar) => [bar.minuteIdx, bar]))
  // Koše za koncem naměřených dat: wall-clock hranice → index koše (v pořadí času)
  const edgeStartMs: number[] = []
  const edgeIndex = new Map<number, number>()
  const lastMeasuredStart = plan.startMs ? plan.startMs[buckets - 1] : Number.NaN
  const bucketOfLive = (bar: PriceBar): number => {
    if (bar.minuteIdx < gridMinutes) return plan.bucketOf[bar.minuteIdx]
    const iso = live.minutesIso[bar.minuteIdx - gridMinutes]
    const ms = iso === undefined ? Number.NaN : Date.parse(iso)
    // Osa bez ISO časů (demo den) → dosavadní indexové koše
    if (!plan.startMs || Number.isNaN(ms)) return Math.floor(bar.minuteIdx / bucketMinutes)
    const start = bucketStartMs(ms, bucketMinutes)
    if (start === lastMeasuredStart) return buckets - 1 // rozdělaná minuta patří do posledního koše
    const known = edgeIndex.get(start)
    if (known !== undefined) return known
    const bucketIdx = buckets + edgeStartMs.length
    edgeStartMs.push(start)
    edgeIndex.set(start, bucketIdx)
    return bucketIdx
  }
  const byBucket = new Map<number, PriceBar[]>()
  for (const bar of [...live.bars].sort((a, b) => a.minuteIdx - b.minuteIdx)) {
    const bucketIdx = bucketOfLive(bar)
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
    // Popisek potřebují jen koše za koncem gridu (náběžná hrana); popisek je
    // hranice koše, ne první živá minuta v něm (#584)
    if (bucketIdx >= buckets) {
      const start = edgeStartMs[bucketIdx - buckets]
      labels[bucketIdx - buckets] =
        start === undefined
          ? (live.labels[first.minuteIdx - gridMinutes] ?? '')
          : minuteLabel(new Date(start).toISOString())
    }
  }
  return { bars, labels, minutesIso: edgeStartMs.map((ms) => new Date(ms).toISOString()) }
}

function aggregateOverlays(overlays: OverlayData, plan: BucketPlan): OverlayData {
  const line = (item: LevelLine): LevelLine => ({
    ...item,
    series: lastNonNull(item.series, plan),
    // Slabé úseky zdí (ADR-0010) se agregují stejně, jinak by indexy košů nesedly
    weak: item.weak ? lastNonNull(item.weak, plan) : undefined,
  })
  return {
    ...overlays,
    price: overlays.price ? aggregateBars(overlays.price, plan) : undefined,
    levels: overlays.levels?.map(line),
    walls: overlays.walls?.map(line),
    sessions: overlays.sessions?.map((session) => ({
      ...session,
      minuteIdx: plan.bucketOf[session.minuteIdx] ?? plan.buckets - 1,
    })),
  }
}

/** Celý den agregovaný do timeframe košů; bucketMinutes ≤ 1 vrací originál. */
export function aggregateDay(day: DayData, bucketMinutes: number): DayData {
  if (bucketMinutes <= 1) return day
  const { minutes, strikes } = day.grid
  const strikeCount = strikes.length
  const plan = cachedBucketPlan(day.minutesIso, minutes, bucketMinutes)
  const buckets = plan.buckets

  const grid: HeatmapGrid = {
    minutes: buckets,
    strikes,
    layers: {
      call: aggregateLayer(day.grid.layers.call, minutes, strikeCount, plan),
      put: aggregateLayer(day.grid.layers.put, minutes, strikeCount, plan),
      signed: aggregateLayer(day.grid.layers.signed, minutes, strikeCount, plan),
    },
    staleAge: day.grid.staleAge
      ? (aggregateLayer(day.grid.staleAge, minutes, strikeCount, plan) ?? null)
      : null,
  }

  // Koš přebírá profil poslední minuty koše S DATY — koš končící v díře (minuta
  // bez snapshotu vrací []) scanuje zpět jako gexProfile/ladder, jinak by profil
  // zhasl, i když dřívější minuty koše měřené jsou (#503).
  const source = day.profileByMinute
  const profileByMinute = source
    ? {
        length: buckets,
        rowsAt: (bucketIdx: number) => {
          for (
            let minuteIdx = plan.ends[bucketIdx];
            minuteIdx >= plan.starts[bucketIdx];
            minuteIdx -= 1
          ) {
            // prettier-ignore
            const rows = source.rowsAt(minuteIdx)
            if (rows.length > 0) return rows
          }
          return []
        },
      }
    : null

  // Koš přebírá FA profil poslední minuty s daty (#232) — jako gexProfile
  const lastRowPerBucket = <T>(rows: (T | null)[] | null): (T | null)[] | null =>
    rows
      ? Array.from({ length: buckets }, (_, bucketIdx) => {
          for (
            let minuteIdx = plan.ends[bucketIdx];
            minuteIdx >= plan.starts[bucketIdx];
            minuteIdx -= 1
          ) {
            // prettier-ignore
            const row = rows[minuteIdx]
            if (row) return row
          }
          return null
        })
      : null

  return {
    source: day.source,
    grid,
    raw: day.raw, // surová 1m matice se nese dál (módy se aplikují před agregací)
    rawFa: day.rawFa, // FA matice stejně — zdroj OI se přepíná před agregací (#232)
    minutesIso: day.minutesIso, // ISO minut zůstávají 1m — zarovnávání je před agregací
    overlays: aggregateOverlays(day.overlays, plan),
    panels: {
      vol: sumSeries(day.panels.vol, plan),
      optVolCall: sumSeries(day.panels.optVolCall, plan),
      optVolPut: sumSeries(day.panels.optVolPut, plan),
      cumDelta: lastSeries(day.panels.cumDelta, plan),
      deltaFlowCall: sumSeries(day.panels.deltaFlowCall, plan),
      deltaFlowPut: sumSeries(day.panels.deltaFlowPut, plan),
      // Evo OI (#573) je úroveň, ne tok — koš přebírá poslední hodnotu
      evoOiCall: lastSeries(day.panels.evoOiCall ?? [], plan),
      evoOiPut: lastSeries(day.panels.evoOiPut ?? [], plan),
    },
    profileByMinute,
    demoProfileRows: day.demoProfileRows,
    // Koš přebírá Dyn GEX profil poslední minuty s daty (ADR-0009)
    gexProfile: lastRowPerBucket(day.gexProfile),
    // Modelované pole se mapuje časem, ne koši — beze změny (ADR-0009 fáze 2)
    gexField: day.gexField,
    // FA varianty (#232): profil po koších jako měřený, pole beze změny
    gexProfileFa: lastRowPerBucket(day.gexProfileFa),
    gexFieldFa: day.gexFieldFa,
    // Koš přebírá žebřík poslední minuty s daty (#244) — jako gexProfile
    ladder: lastRowPerBucket(day.ladder),
    spotSeries: lastNonNull(day.spotSeries, plan),
    // Popisek koše = jeho wall-clock hranice (11:00), i když první naměřená
    // minuta v koši je až 11:02 (#584); bez ISO osy zbývá popisek první minuty
    minuteLabels: Array.from({ length: buckets }, (_, bucketIdx) =>
      plan.startMs
        ? minuteLabel(new Date(plan.startMs[bucketIdx]).toISOString())
        : (day.minuteLabels[plan.starts[bucketIdx]] ?? ''),
    ),
    lastMinuteIso: day.lastMinuteIso,
  }
}
