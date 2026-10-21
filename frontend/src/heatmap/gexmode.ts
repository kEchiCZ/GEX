/** Dyn GEX heatmap mód (ADR-0009 fáze 2): 2D pole modelovaného NetGEX.

Minulé sloupce = uložená historie 1D profilů (poctivé: tehdejší IV/OI/τ),
budoucí sloupce = pole `gexfield` z posledního snapshotu s klesajícím τ.
Obě části sdílejí jmenovatel normalizace (p99 naměřené části), aby projekce
nepřebila minulost jinou škálou. Budoucí sloupce se kreslí ve stávající
projekční zóně (ztlumení + předěl, ADR-0006) — vizuál sám říká „model".
*/
import type { GexFieldRow, GexProfileRow } from '../replay/loader'
import { bucketStartMs } from './buckets'
import { dataMinutesOf } from './grid'
import type { HeatmapGrid } from './grid'
import { copysignTransform, p99Denominator } from './modes'
import type { HeatmapScale } from './modes'
import { projectGrid } from './projection'
import { priceWeight } from './units'
import type { PlaneUnits } from './units'

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

/** Naměřená část: forward-fill profilů per minuta, vzorky na cenách strikes.

Váha jednotky (#569) se násobí až PO interpolaci — na striku je cena P přesně
známá; interpolovat zvážené hodnoty by dalo jiné (špatné) číslo.
`gapMinutes` (FA profily, ADR-0057 bod 4, #1632): minuta se snímkem, ale bez
FA řad, je mezera — poslední profil se přes ni nedrží. Minuta bez snímku
(díra ve sběru) drží jako u měřených profilů. */
function measuredLayer(
  profiles: (GexProfileRow | null)[],
  strikes: number[],
  minutes: number,
  units: PlaneUnits,
  gapMinutes?: boolean[],
): Float32Array {
  const layer = new Float32Array(minutes * strikes.length)
  let last: GexProfileRow | null = null
  for (let minuteIdx = 0; minuteIdx < minutes; minuteIdx += 1) {
    const row = profiles[minuteIdx] ?? null
    if (row) last = row
    else if (gapMinutes?.[minuteIdx]) last = null
    if (!last) continue
    for (let strikeIdx = 0; strikeIdx < strikes.length; strikeIdx += 1) {
      layer[strikeIdx * minutes + minuteIdx] =
        sampleAt(
          last.values,
          0,
          last.values.length,
          last.gridStart,
          last.gridStep,
          strikes[strikeIdx],
        ) * priceWeight(strikes[strikeIdx], units)
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

/** Profil platný v minutě `index` 1m osy: poslední řádek do ní včetně — stejné
držení jako `measuredLayer`. Minuta z `gapMinutes` (FA mezera, #1632) hledání
zastaví: FA profil se přes mezeru nedrží. */
export function profileAt(
  profiles: (GexProfileRow | null)[],
  index: number,
  gapMinutes?: boolean[],
): GexProfileRow | null {
  for (let i = Math.min(index, profiles.length - 1); i >= 0; i -= 1) {
    const row = profiles[i]
    if (row) return row
    if (gapMinutes?.[i]) return null
  }
  return null
}

/** FA modelované pole pro projekci, jen když je z téže minuty jako měřené
(nebo novější). Starší FA pole = FA je teď v mezeře a projekce by držela odhad
z doby před ní (ADR-0057 bod 4, #1632); bez měřeného pole platí FA. */
export function currentFaField(
  measured: GexFieldRow | null,
  fa: GexFieldRow | null,
): GexFieldRow | null {
  if (fa === null) return null
  return measured === null || fa.tsIso >= measured.tsIso ? fa : null
}

/** Jmenovatel normalizace dyn módu — p99 naměřené části (sdílí ho i projekce).

`units` (#569) default `per_point` = uložená jednotka enginu (bez váhy). */
export function gexDenominator(
  profiles: (GexProfileRow | null)[],
  strikes: number[],
  scale: HeatmapScale,
  units: PlaneUnits = 'per_point',
  gapMinutes?: boolean[],
): number {
  const layer = transformInPlace(
    measuredLayer(profiles, strikes, profiles.length, units, gapMinutes),
    scale,
  )
  return Math.max(p99Denominator(layer), 1e-9)
}

/** Sestaví naměřenou část dyn módu (signed vrstva −1..1) — čistá funkce.

`units` (#569, #1401): váha jednotky (P²/100, nebo × P u dolarové delty) se aplikuje
před transformací škály i p99, takže
barvy, kontury i computedWalls čtou konzistentně zvážené pole; `per_point`
(default) je bitově shodný s chováním před #569. */
export function buildGexGrid(
  profiles: (GexProfileRow | null)[],
  strikes: number[],
  minutes: number,
  scale: HeatmapScale,
  units: PlaneUnits = 'per_point',
  gapMinutes?: boolean[],
): HeatmapGrid {
  const layer = transformInPlace(
    measuredLayer(profiles, strikes, minutes, units, gapMinutes),
    scale,
  )
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
    /** Jednotka zobrazení (#569); default per_point = uložená jednotka enginu. */
    units?: PlaneUnits
    /** FA mezery 1m osy (#1632) — jmenovatel jako buildGexGrid. */
    gapMinutes?: boolean[]
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

  const units = opts.units ?? 'per_point'
  const denominator = gexDenominator(
    opts.profiles,
    grid.strikes,
    opts.scale,
    units,
    opts.gapMinutes,
  )
  const strikeCount = grid.strikes.length
  const total = dataMinutes + extra
  const signed = grid.layers.signed ?? new Float32Array(grid.minutes * strikeCount)
  const result = new Float32Array(total * strikeCount)
  for (let strikeIdx = 0; strikeIdx < strikeCount; strikeIdx += 1) {
    const from = strikeIdx * grid.minutes
    result.set(signed.subarray(from, from + dataMinutes), strikeIdx * total)
  }
  const colStepMs = Math.max(1, fieldRow.colStepMin) * 60_000
  const bucketMs = Math.max(1, opts.bucketMinutes) * 60_000
  // Koš k pokrývá čas hranice posledního koše + (k+1) × bucket — shodně
  // s `projectionLabels` a `projectedSessions` (#584)
  const lastBucketStart = bucketStartMs(lastMs, opts.bucketMinutes)
  for (let k = 0; k < extra; k += 1) {
    const timeMs = lastBucketStart + (k + 1) * bucketMs
    const colIdx = Math.min(
      fieldRow.colCount - 1,
      Math.max(0, Math.round((timeMs - colStartMs) / colStepMs)),
    )
    const offset = colIdx * gridLen
    for (let strikeIdx = 0; strikeIdx < strikeCount; strikeIdx += 1) {
      // Váha jednotky (#569) až po interpolaci — P je cena striku, ne uzlu pole
      const raw =
        sampleAt(
          fieldRow.values,
          offset,
          gridLen,
          fieldRow.gridStart,
          fieldRow.gridStep,
          grid.strikes[strikeIdx],
        ) * priceWeight(grid.strikes[strikeIdx], units)
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
