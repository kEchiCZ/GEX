/** Dyn GEX heatmap mód (ADR-0009 fáze 2): 2D pole modelovaného NetGEX.

Minulé sloupce = uložená historie 1D profilů (poctivé: tehdejší IV/OI/τ),
budoucí sloupce = pole `gexfield` z posledního snapshotu s klesajícím τ.
Obě části sdílejí jmenovatel normalizace (p99 naměřené části), aby projekce
nepřebila minulost jinou škálou. Budoucí sloupce se kreslí ve stávající
projekční zóně (ztlumení + předěl, ADR-0006) — vizuál sám říká „model".
*/
import type { GexFieldRow, GexProfileRow } from '../replay/loader'
import { dataMinutesOf } from './grid'
import type { HeatmapGrid } from './grid'
import { copysignTransform, p99Denominator } from './modes'
import type { HeatmapScale } from './modes'
import { projectGrid } from './projection'

/** Lineární interpolace hodnoty na ceně `price`; mimo mřížku hodnota kraje.

`offset`/`length` vymezují sloupec ve zřetězeném poli (gexfield). */
function sampleAt(
  values: number[],
  offset: number,
  length: number,
  gridStart: number,
  gridStep: number,
  price: number,
): number {
  if (length <= 0 || gridStep <= 0) return 0
  const pos = (price - gridStart) / gridStep
  if (pos <= 0) return values[offset]
  if (pos >= length - 1) return values[offset + length - 1]
  const low = Math.floor(pos)
  const frac = pos - low
  return values[offset + low] * (1 - frac) + values[offset + low + 1] * frac
}

/** Naměřená část: forward-fill profilů per minuta, vzorky na cenách strikes. */
function measuredLayer(
  profiles: (GexProfileRow | null)[],
  strikes: number[],
  minutes: number,
): Float32Array {
  const layer = new Float32Array(minutes * strikes.length)
  let last: GexProfileRow | null = null
  for (let minuteIdx = 0; minuteIdx < minutes; minuteIdx += 1) {
    const row = profiles[minuteIdx] ?? null
    if (row) last = row
    if (!last) continue
    for (let strikeIdx = 0; strikeIdx < strikes.length; strikeIdx += 1) {
      layer[strikeIdx * minutes + minuteIdx] = sampleAt(
        last.values,
        0,
        last.values.length,
        last.gridStart,
        last.gridStep,
        strikes[strikeIdx],
      )
    }
  }
  return layer
}

function transformInPlace(layer: Float32Array, scale: HeatmapScale): Float32Array {
  if (scale === 'linear') return layer
  for (let index = 0; index < layer.length; index += 1) {
    layer[index] = copysignTransform(layer[index], scale)
  }
  return layer
}

/** Jmenovatel normalizace dyn módu — p99 naměřené části (sdílí ho i projekce). */
export function gexDenominator(
  profiles: (GexProfileRow | null)[],
  strikes: number[],
  scale: HeatmapScale,
): number {
  const layer = transformInPlace(measuredLayer(profiles, strikes, profiles.length), scale)
  return Math.max(p99Denominator(layer), 1e-9)
}

/** Sestaví naměřenou část dyn módu (signed vrstva −1..1) — čistá funkce. */
export function buildGexGrid(
  profiles: (GexProfileRow | null)[],
  strikes: number[],
  minutes: number,
  scale: HeatmapScale,
): HeatmapGrid {
  const layer = transformInPlace(measuredLayer(profiles, strikes, minutes), scale)
  const denominator = Math.max(p99Denominator(layer), 1e-9)
  for (let index = 0; index < layer.length; index += 1) {
    const value = layer[index] / denominator
    layer[index] = value < -1 ? -1 : value > 1 ? 1 : value
  }
  return { minutes, strikes, layers: { signed: layer }, staleAge: null }
}

/** Rozšíří dyn grid o `extra` projekčních košů z modelovaného pole.

Koš `k` se mapuje na nejbližší sloupec pole podle času; bez pole (starší
engine, výpadek) spadne na konstantní projekci (ADR-0006). `profiles` jsou
1m řádky PŘED agregací — jmenovatel musí vyjít stejně jako v buildGexGrid. */
export function projectGexField(
  grid: HeatmapGrid,
  extra: number,
  fieldRow: GexFieldRow | null,
  opts: {
    profiles: (GexProfileRow | null)[]
    lastMinuteIso: string | null
    bucketMinutes: number
    scale: HeatmapScale
  },
): HeatmapGrid {
  const dataMinutes = dataMinutesOf(grid)
  if (extra <= 0 || dataMinutes === 0) return grid
  const colStartMs = fieldRow ? new Date(fieldRow.colStartIso).getTime() : Number.NaN
  const lastMs = opts.lastMinuteIso ? new Date(opts.lastMinuteIso).getTime() : Number.NaN
  const gridLen = fieldRow ? fieldRow.values.length / fieldRow.colCount : 0
  if (
    !fieldRow ||
    fieldRow.colCount <= 0 ||
    !Number.isInteger(gridLen) ||
    gridLen <= 0 ||
    !Number.isFinite(colStartMs) ||
    !Number.isFinite(lastMs)
  ) {
    return projectGrid(grid, extra)
  }

  const denominator = gexDenominator(opts.profiles, grid.strikes, opts.scale)
  const strikeCount = grid.strikes.length
  const total = dataMinutes + extra
  const signed = grid.layers.signed ?? new Float32Array(grid.minutes * strikeCount)
  const result = new Float32Array(total * strikeCount)
  for (let strikeIdx = 0; strikeIdx < strikeCount; strikeIdx += 1) {
    const from = strikeIdx * grid.minutes
    result.set(signed.subarray(from, from + dataMinutes), strikeIdx * total)
  }
  const colStepMs = Math.max(1, fieldRow.colStepMin) * 60_000
  for (let k = 0; k < extra; k += 1) {
    const timeMs = lastMs + (k + 1) * opts.bucketMinutes * 60_000
    const colIdx = Math.min(
      fieldRow.colCount - 1,
      Math.max(0, Math.round((timeMs - colStartMs) / colStepMs)),
    )
    const offset = colIdx * gridLen
    for (let strikeIdx = 0; strikeIdx < strikeCount; strikeIdx += 1) {
      const raw = sampleAt(
        fieldRow.values,
        offset,
        gridLen,
        fieldRow.gridStart,
        fieldRow.gridStep,
        grid.strikes[strikeIdx],
      )
      const value = copysignTransform(raw, opts.scale) / denominator
      result[strikeIdx * total + dataMinutes + k] = value < -1 ? -1 : value > 1 ? 1 : value
    }
  }
  return {
    minutes: total,
    dataMinutes,
    projectionDynamic: true,
    strikes: grid.strikes,
    layers: { signed: result },
    staleAge: null,
  }
}
