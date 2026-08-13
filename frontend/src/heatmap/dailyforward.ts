/** Denní Forward GEX (#572): projekční sloupce, útesy a hranice expirací.

Čtecí polovina #519: API dodá bloky per budoucí obchodní den (pole z dnešního
OI mínus expirace, které do té doby odpadnou). Tady se z bloků skládá:

- rozšíření Daily osy o budoucí dny (měřené módy se NEprojektují — sloupce
  jsou prázdné, drží princip ADR-0006; model nese jen Dyn podklad),
- Dyn podklad budoucích dnů (sdílený jmenovatel s naměřenou částí),
- hranice expirací pro overlay (svislice + popisek „−38 % gammy") a pro
  blur — útes je signál, vyhlazování ho nesmí rozmazat (Nález 8: jedna
  `constantFromX` hranice nestačí, hranic je víc → `hardEdgesX`).
*/
import { dayLabel } from '../replay/daily'
import type { GexProfileRow } from '../replay/loader'
import { dataMinutesOf } from './grid'
import type { HeatmapGrid } from './grid'
import { gexDenominator } from './gexmode'
import { copysignTransform } from './modes'
import type { HeatmapScale } from './modes'
import { priceWeight } from './units'
import type { GexUnits } from './units'

/** Blok jednoho budoucího dne z GET /gexforward/{symbol}. */
export interface ForwardBlock {
  day: string // ISO datum
  gridStart: number
  gridStep: number
  values: number[]
  droppedExpiries: string[] // YYYYMMDD odpadlé mezi předchozím dnem a tímto
  droppedShare: number | null
  ivFallbackShare: number
}

/** Rozsah projekce (#519): settle = žádné budoucí dny, +1 den, do konce týdne. */
export type ForwardRange = 'settle' | 'plus1' | 'week'
export const FORWARD_RANGES: readonly ForwardRange[] = ['settle', 'plus1', 'week']
export const FORWARD_RANGE_LABELS: Record<ForwardRange, string> = {
  settle: 'settle',
  plus1: '+1 den',
  week: 'týden',
}

/** OPEX = třetí pátek v měsíci — sytější a silnější svislice (#572). */
export function isOpexExpiry(expiry: string): boolean {
  const year = Number(expiry.slice(0, 4))
  const month = Number(expiry.slice(4, 6))
  const day = Number(expiry.slice(6, 8))
  const date = new Date(Date.UTC(year, month - 1, day))
  if (date.getUTCDay() !== 5) return false
  return day >= 15 && day <= 21 // třetí pátek padne vždy do 15.–21.
}

/** Budoucí bloky za posledním naměřeným dnem, ořezané rozsahem projekce. */
export function futureBlocks(
  blocks: ForwardBlock[],
  lastMeasuredDate: string,
  range: ForwardRange,
): ForwardBlock[] {
  if (range === 'settle' || !lastMeasuredDate) return []
  const future = blocks
    .filter((block) => block.day > lastMeasuredDate)
    .sort((a, b) => a.day.localeCompare(b.day))
  return range === 'plus1' ? future.slice(0, 1) : future
}

export interface ForwardBoundary {
  /** Sloupec, PŘED kterým expirace odpadly — svislice na jeho levé hraně. */
  minuteIdx: number
  expiries: string[]
  share: number | null
  isOpex: boolean
}

/** Hranice expirací pro overlay a blur; jen bloky, kde něco odpadlo. */
export function forwardBoundaries(blocks: ForwardBlock[], dataColumns: number): ForwardBoundary[] {
  return blocks
    .map((block, index) => ({ block, minuteIdx: dataColumns + index }))
    .filter(({ block }) => block.droppedExpiries.length > 0)
    .map(({ block, minuteIdx }) => ({
      minuteIdx,
      expiries: block.droppedExpiries,
      share: block.droppedShare,
      isOpex: block.droppedExpiries.some(isOpexExpiry),
    }))
}

/** Popisky osy X budoucích dnů. */
export function forwardLabels(blocks: ForwardBlock[]): string[] {
  return blocks.map((block) => dayLabel(block.day))
}

/** Rozšíří MĚŘENÝ Daily grid o prázdné projekční sloupce.

Měřené módy se do budoucna neprojektují (ADR-0006 drží i tady) — sloupce
jsou průhledné a model nese výhradně Dyn podklad pod nimi. */
export function extendDailyGrid(grid: HeatmapGrid, extra: number): HeatmapGrid {
  const dataMinutes = dataMinutesOf(grid)
  if (extra <= 0 || dataMinutes === 0) return grid
  const strikeCount = grid.strikes.length
  const total = dataMinutes + extra
  const extend = (layer: Float32Array | undefined): Float32Array | undefined => {
    if (!layer) return undefined
    const result = new Float32Array(total * strikeCount)
    for (let strikeIdx = 0; strikeIdx < strikeCount; strikeIdx += 1) {
      const from = strikeIdx * grid.minutes
      result.set(layer.subarray(from, from + dataMinutes), strikeIdx * total)
    }
    return result
  }
  return {
    minutes: total,
    dataMinutes,
    // Sloupce projekce jsou různé (per den) — vypnout zkratky konstantní projekce
    projectionDynamic: true,
    strikes: grid.strikes,
    layers: {
      call: extend(grid.layers.call),
      put: extend(grid.layers.put),
      signed: extend(grid.layers.signed),
    },
    staleAge: null,
    missingMinutes: grid.missingMinutes
      ? [...grid.missingMinutes, ...Array.from({ length: extra }, () => false)]
      : null,
  }
}

/** Lineární interpolace hodnoty bloku na ceně (kopie sémantiky gexmode). */
function sampleBlock(block: ForwardBlock, price: number): number {
  const length = block.values.length
  if (length === 0 || block.gridStep <= 0) return 0
  const pos = (price - block.gridStart) / block.gridStep
  if (pos <= 0) return block.values[0]
  if (pos >= length - 1) return block.values[length - 1]
  const low = Math.floor(pos)
  const frac = pos - low
  return block.values[low] * (1 - frac) + block.values[low + 1] * frac
}

/** Rozšíří Dyn Daily podklad o sloupce budoucích dnů z forward bloků.

Jmenovatel normalizace = p99 naměřené části (sdílený s `buildGexGrid`),
váha jednotky (#569) po interpolaci. `hardEdgesX` nese hranice expirací —
blur je nesmí přelít (útes je signál, ne šum). */
export function projectDailyForward(
  grid: HeatmapGrid,
  blocks: ForwardBlock[],
  opts: {
    profiles: (GexProfileRow | null)[]
    scale: HeatmapScale
    units?: GexUnits
  },
): HeatmapGrid {
  const dataMinutes = dataMinutesOf(grid)
  if (blocks.length === 0 || dataMinutes === 0) return grid
  const units = opts.units ?? 'per_point'
  const denominator = gexDenominator(opts.profiles, grid.strikes, opts.scale, units)
  const strikeCount = grid.strikes.length
  const total = dataMinutes + blocks.length
  const signed = grid.layers.signed ?? new Float32Array(grid.minutes * strikeCount)
  const result = new Float32Array(total * strikeCount)
  for (let strikeIdx = 0; strikeIdx < strikeCount; strikeIdx += 1) {
    const from = strikeIdx * grid.minutes
    result.set(signed.subarray(from, from + dataMinutes), strikeIdx * total)
  }
  blocks.forEach((block, index) => {
    for (let strikeIdx = 0; strikeIdx < strikeCount; strikeIdx += 1) {
      const raw =
        sampleBlock(block, grid.strikes[strikeIdx]) * priceWeight(grid.strikes[strikeIdx], units)
      const value = copysignTransform(raw, opts.scale) / denominator
      result[strikeIdx * total + dataMinutes + index] = value < -1 ? -1 : value > 1 ? 1 : value
    }
  })
  return {
    minutes: total,
    dataMinutes,
    projectionDynamic: true,
    strikes: grid.strikes,
    layers: { signed: result },
    staleAge: null,
    hardEdgesX: forwardBoundaries(blocks, dataMinutes).map((boundary) => boundary.minuteIdx),
  }
}
