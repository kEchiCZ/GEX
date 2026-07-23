/** Heatmap módy a škály (SPEC 4.3) — TS zrcadlo engine compute/heatmap.py.

Přepnutí módu/škály je čistý přepočet v paměti nad surovou snapshot maticí
(žádný fetch — SPEC kap. 8, latence < 100 ms). Sémantika 1:1 s enginem:
OTM: call K > S, put K < S; ITM je doplněk (ATM buňka patří do ITM vrstvy).
Škály zachovávají znaménko (copysign), normalizace p99 celého dne.
*/
import type { HeatmapGrid } from './grid'

export const HEATMAP_MODES = [
  { value: 'oi', label: 'OI' },
  { value: 'vol_otm', label: 'Vol OTM' },
  { value: 'vol_itm', label: 'Vol ITM' },
  { value: 'vol_signed', label: 'Vol ±' },
  { value: 'oi_plus_otm', label: 'OI+OTM' },
  { value: 'oi_minus_itm', label: 'OI−ITM' },
  { value: 'oi_signed_all', label: 'OI±All' },
  // VEX (#201): vega × OI — kde bolí pohyb volatility (volatility walls)
  { value: 'vex', label: 'VEX' },
  { value: 'vex_signed', label: 'VEX ±' },
] as const
/** Dyn GEX už není mód, ale samostatný overlay přepínač (#242, à la Moodix) —
    hodnota v typu zůstává kvůli gridům z gexmode.ts a persistovaným volbám. */
export type HeatmapMode = (typeof HEATMAP_MODES)[number]['value'] | 'dyn_gex'
/** Módy počítané ze surové snapshot matice — Dyn GEX má vlastní stavbu (gexmode.ts). */
export type MeasuredHeatmapMode = Exclude<HeatmapMode, 'dyn_gex'>

export const HEATMAP_SCALES = [
  { value: 'linear', label: 'Linear' },
  { value: 'sqrt', label: '√' },
  { value: 'log', label: 'Log' },
  { value: 'cbrt', label: 'Pow⅓' },
] as const
export type HeatmapScale = (typeof HEATMAP_SCALES)[number]['value']

/** Surová snapshot matice dne (index = strikeIdx * minutes + minuteIdx). */
export interface RawDay {
  minutes: number
  strikes: number[]
  callOi: Float32Array
  putOi: Float32Array
  callVolume: Float32Array
  putVolume: Float32Array
  /** Vega matice pro VEX módy (#201); starší data je nemají → nulová vrstva. */
  callVega?: Float32Array
  putVega?: Float32Array
  /** Spot per minuta (OTM/ITM klasifikace) — díry se forward-fillují. */
  spotSeries: (number | null)[]
  staleAge: Float32Array | null
}

/** Váhy OI+OTM blendu (SPEC 4.3, stejné defaulty jako engine). */
const OI_WEIGHT = 0.6
const VOL_WEIGHT = 0.4

export function copysignTransform(value: number, scale: HeatmapScale): number {
  const magnitude = Math.abs(value)
  const sign = value < 0 ? -1 : 1
  if (scale === 'sqrt') return sign * Math.sqrt(magnitude)
  if (scale === 'log') return sign * Math.log1p(magnitude)
  if (scale === 'cbrt') return sign * magnitude ** (1 / 3)
  return value
}

/** p99 absolutních hodnot — robustní jmenovatel normalizace (SPEC 4.3).

Hledá se JEDEN kvantil, ne celé pořadí, takže stačí quickselect (Hoare) nad kopií
typed pole — O(n) místo O(n log n) a bez boxování do JS pole. Plné třídění dne
bylo ~98 % nákladu skládání gridu (#142): 256k buněk 108 ms → 2,5 ms. */
export function p99Denominator(values: Float32Array): number {
  const count = values.length
  if (count === 0) return 0
  const magnitudes = new Float32Array(count)
  for (let index = 0; index < count; index += 1) {
    const value = Math.abs(values[index])
    magnitudes[index] = Number.isFinite(value) ? value : 0
  }
  const target = Math.max(0, Math.ceil(0.99 * count) - 1)
  let low = 0
  let high = count - 1
  while (low < high) {
    const pivot = magnitudes[(low + high) >> 1]
    let left = low
    let right = high
    while (left <= right) {
      while (magnitudes[left] < pivot) left += 1
      while (magnitudes[right] > pivot) right -= 1
      if (left <= right) {
        const swap = magnitudes[left]
        magnitudes[left] = magnitudes[right]
        magnitudes[right] = swap
        left += 1
        right -= 1
      }
    }
    if (target <= right) high = right
    else if (target >= left) low = left
    else break // cíl padl mezi oddíly — na své pozici už je hledaná hodnota
  }
  return magnitudes[target]
}

/** Forward-fill spotů; minuty před první hodnotou dostanou první známý spot. */
function filledSpots(spotSeries: (number | null)[], minutes: number): (number | null)[] {
  const result: (number | null)[] = Array.from({ length: minutes }, () => null)
  let last: number | null = null
  for (let minuteIdx = 0; minuteIdx < minutes; minuteIdx += 1) {
    const value = spotSeries[minuteIdx]
    if (value !== null && value !== undefined) last = value
    result[minuteIdx] = last
  }
  const first = result.find((value) => value !== null) ?? null
  return result.map((value) => value ?? first)
}

/** Sestaví grid vrstvy pro daný mód a škálu (čistá funkce — testy proti enginu). */
export function buildModeGrid(
  raw: RawDay,
  mode: MeasuredHeatmapMode,
  scale: HeatmapScale,
): HeatmapGrid {
  const { minutes, strikes } = raw
  const size = minutes * strikes.length
  const spots = filledSpots(raw.spotSeries, minutes)

  // Fallback: dokud ranní OI nedorazilo (ADR-0001), OI složky nahrazuje volume
  const totalOi = raw.callOi.reduce((sum, v) => sum + v, 0) + raw.putOi.reduce((s, v) => s + v, 0)
  const callOi = totalOi > 0 ? raw.callOi : raw.callVolume
  const putOi = totalOi > 0 ? raw.putOi : raw.putVolume

  const call = new Float32Array(size)
  const put = new Float32Array(size)
  const signed = new Float32Array(size)
  const twoSided = mode !== 'vol_signed' && mode !== 'oi_signed_all' && mode !== 'vex_signed'
  // VEX (#201): vega chybí ve starších bundle/demo datech → nulové vrstvy
  const callVega = raw.callVega ?? new Float32Array(size)
  const putVega = raw.putVega ?? new Float32Array(size)

  for (let minuteIdx = 0; minuteIdx < minutes; minuteIdx += 1) {
    const spot = spots[minuteIdx]
    // OI+OTM: složky se normalizují na společné maximum per minuta (jako engine per snapshot)
    let maxOi = 0
    let maxOtm = 0
    if (mode === 'oi_plus_otm') {
      for (let strikeIdx = 0; strikeIdx < strikes.length; strikeIdx += 1) {
        const index = strikeIdx * minutes + minuteIdx
        maxOi = Math.max(maxOi, callOi[index], putOi[index])
        if (spot !== null && strikes[strikeIdx] > spot) {
          maxOtm = Math.max(maxOtm, raw.callVolume[index])
        }
        if (spot !== null && strikes[strikeIdx] < spot) {
          maxOtm = Math.max(maxOtm, raw.putVolume[index])
        }
      }
    }
    for (let strikeIdx = 0; strikeIdx < strikes.length; strikeIdx += 1) {
      const strike = strikes[strikeIdx]
      const index = strikeIdx * minutes + minuteIdx
      const callOtm = spot !== null && strike > spot ? raw.callVolume[index] : 0
      const putOtm = spot !== null && strike < spot ? raw.putVolume[index] : 0
      const callItm = spot !== null && strike <= spot ? raw.callVolume[index] : 0
      const putItm = spot !== null && strike >= spot ? raw.putVolume[index] : 0

      if (mode === 'oi') {
        call[index] = callOi[index]
        put[index] = putOi[index]
      } else if (mode === 'vol_otm') {
        call[index] = callOtm
        put[index] = putOtm
      } else if (mode === 'vol_itm') {
        call[index] = callItm
        put[index] = putItm
      } else if (mode === 'vol_signed') {
        signed[index] = raw.callVolume[index] - raw.putVolume[index]
      } else if (mode === 'oi_plus_otm') {
        const blend = (oi: number, otm: number): number =>
          OI_WEIGHT * (maxOi > 0 ? oi / maxOi : 0) + VOL_WEIGHT * (maxOtm > 0 ? otm / maxOtm : 0)
        call[index] = blend(callOi[index], callOtm)
        put[index] = blend(putOi[index], putOtm)
      } else if (mode === 'oi_minus_itm') {
        call[index] = callOi[index] - callItm
        put[index] = putOi[index] - putItm
      } else if (mode === 'vex') {
        call[index] = callVega[index] * raw.callOi[index]
        put[index] = putVega[index] * raw.putOi[index]
      } else if (mode === 'vex_signed') {
        signed[index] = callVega[index] * raw.callOi[index] - putVega[index] * raw.putOi[index]
      } else {
        signed[index] = callOi[index] - putOi[index]
      }
    }
  }

  // Škálování i normalizace na místě prostou smyčkou — `Float32Array.from` s callbackem
  // stálo přes 250k buněk desítky ms navíc (#142).
  const transform = (layer: Float32Array): Float32Array => {
    if (scale === 'linear') return layer
    for (let index = 0; index < layer.length; index += 1) {
      layer[index] = copysignTransform(layer[index], scale)
    }
    return layer
  }
  const normalize = (layer: Float32Array, denominator: number, floor: number): Float32Array => {
    for (let index = 0; index < layer.length; index += 1) {
      const value = layer[index] / denominator
      layer[index] = value < floor ? floor : value > 1 ? 1 : value
    }
    return layer
  }

  if (twoSided) {
    const callScaled = transform(call)
    const putScaled = transform(put)
    const denominator = Math.max(p99Denominator(callScaled), p99Denominator(putScaled), 1e-9)
    return {
      minutes,
      strikes,
      layers: {
        call: normalize(callScaled, denominator, 0),
        put: normalize(putScaled, denominator, 0),
      },
      staleAge: raw.staleAge,
    }
  }
  const signedScaled = transform(signed)
  const denominator = Math.max(p99Denominator(signedScaled), 1e-9)
  return {
    minutes,
    strikes,
    layers: { signed: normalize(signedScaled, denominator, -1) },
    staleAge: raw.staleAge,
  }
}
