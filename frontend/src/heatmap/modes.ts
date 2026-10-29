/** Heatmap módy a škály (SPEC 4.3) — TS zrcadlo engine compute/heatmap.py.

Přepnutí módu/škály je čistý přepočet v paměti nad surovou snapshot maticí
(žádný fetch — SPEC kap. 8, latence < 100 ms). Sémantika 1:1 s enginem:
OTM: call K > S, put K < S; ITM je doplněk (ATM buňka patří do ITM vrstvy).
Škály zachovávají znaménko (copysign), normalizace p99 celého dne.
*/
import { perfEnd, perfStart } from '../perf/meter'
import type { HeatmapGrid } from './grid'
import { selectKth } from './select'

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
/** Dyn GEX už není mód, ale samostatný overlay přepínač (#242) —
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

/** Surová snapshot matice dne (index = strikeIdx * stride + minuteIdx). */
export interface RawDay {
  minutes: number
  /** Stride řádku matic (#515): kapacitní rezerva pro živé appendy.
  Chybí-li, matice jsou kompaktní a stride = minutes. INVARIANT: buňky se
  sloupcem ≥ minutes jsou nulové. */
  stride?: number
  strikes: number[]
  callOi: Float32Array
  putOi: Float32Array
  callVolume: Float32Array
  putVolume: Float32Array
  /** Vega matice pro VEX módy (#201); starší data je nemají → nulová vrstva. */
  callVega?: Float32Array
  putVega?: Float32Array
  /** Podíl objemu mimo tisk (#1007): kumulativní tisky a zbytek bez tisku per
  buňka; NaN = neznámé. Chybí u dnů bez řady printvol. */
  callPrinted?: Float32Array
  putPrinted?: Float32Array
  callStructured?: Float32Array
  putStructured?: Float32Array
  /** Spot per minuta (OTM/ITM klasifikace) — díry se forward-fillují. */
  spotSeries: (number | null)[]
  staleAge: Float32Array | null
  /** Minuty se snímkem, ale bez OI vrstvy (FA mezera, ADR-0057 bod 4, #1632):
  módy čtoucí OI tam nekreslí nic, čistě objemové módy beze změny. Chybí = OI
  všude, kde je snímek. */
  oiGapMinutes?: boolean[]
  /** Maska objemu per buňka (1 = volume změřené, 0 = NULL z tasty fallbacku nebo
  chybějící řádek) a minuty se snímkem — objem okna Rozsah ⧉ (#1631). Chybí =
  den bez matice objemu (Daily, demo), okno pak objem nemá. */
  callVolumeKnown?: Float32Array
  putVolumeKnown?: Float32Array
  snapshotMinutes?: boolean[]
}

/** Módy jen z objemu — FA mezera (`oiGapMinutes`) se jich netýká. */
const VOLUME_ONLY_MODES: ReadonlySet<MeasuredHeatmapMode> = new Set([
  'vol_otm',
  'vol_itm',
  'vol_signed',
])

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
/** Dominance stran (#580): poměr p99 silnější / slabší strany dvoustranné mřížky.

Sdílený jmenovatel normalizace se v poměru krátí, takže funguje i nad už
normalizovanými vrstvami (výchozí OI·Linear grid z loaderu). Null = jednostranný
mód, nebo slabší strana nemá co vynořit (p99 ~ 0 — přepnutí škály by nic
neukázalo). */
export function sideDominance(grid: {
  layers: { call?: Float32Array; put?: Float32Array }
}): { ratio: number; side: 'call' | 'put' } | null {
  const { call, put } = grid.layers
  if (!call || !put) return null
  const callP99 = p99Denominator(call)
  const putP99 = p99Denominator(put)
  const weaker = Math.min(callP99, putP99)
  if (weaker <= 1e-9) return null
  return {
    ratio: Math.max(callP99, putP99) / weaker,
    side: callP99 >= putP99 ? 'call' : 'put',
  }
}

/** Práh dominance pro hint na škálu (#580) — návrh z issue: 5 : 1. */
export const SCALE_HINT_RATIO = 5

/** Hint na přepnutí škály (#580): jen na Linear, jen nad prahem, jen dokud ho
uživatel trvale nezavřel. Nikdy škálu nepřepíná — vrací data pro nápovědu. */
export function scaleHintFor(
  grid: { layers: { call?: Float32Array; put?: Float32Array } },
  scale: HeatmapScale,
  dismissed: boolean,
): { ratio: number; side: 'call' | 'put' } | null {
  if (scale !== 'linear' || dismissed) return null
  const dominance = sideDominance(grid)
  return dominance !== null && dominance.ratio >= SCALE_HINT_RATIO ? dominance : null
}

export function p99Denominator(values: Float32Array): number {
  const count = values.length
  if (count === 0) return 0
  const magnitudes = new Float32Array(count)
  for (let index = 0; index < count; index += 1) {
    const value = Math.abs(values[index])
    magnitudes[index] = Number.isFinite(value) ? value : 0
  }
  return selectKth(magnitudes, Math.max(0, Math.ceil(0.99 * count) - 1))
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
  const t0 = perfStart()
  const grid = composeModeGrid(raw, mode, scale)
  perfEnd('buildModeGrid', t0) // měřič výkonu (#1409)
  return grid
}

function composeModeGrid(raw: RawDay, mode: MeasuredHeatmapMode, scale: HeatmapScale): HeatmapGrid {
  const { minutes, strikes } = raw
  // Kapacitní stride (#515): čte se přes něj, výstupní vrstvy jsou kompaktní
  const stride = raw.stride ?? minutes
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
  // (velikost podle stride — čte se strided indexem)
  const callVega = raw.callVega ?? new Float32Array(stride * strikes.length)
  const putVega = raw.putVega ?? new Float32Array(stride * strikes.length)

  const oiGapApplies = raw.oiGapMinutes !== undefined && !VOLUME_ONLY_MODES.has(mode)
  for (let minuteIdx = 0; minuteIdx < minutes; minuteIdx += 1) {
    // Minuta bez OI vrstvy zůstane prázdná (nuly = mezera jako díra ve sběru)
    if (oiGapApplies && raw.oiGapMinutes?.[minuteIdx]) continue
    const spot = spots[minuteIdx]
    // OI+OTM: složky se normalizují na společné maximum per minuta (jako engine per snapshot)
    let maxOi = 0
    let maxOtm = 0
    if (mode === 'oi_plus_otm') {
      for (let strikeIdx = 0; strikeIdx < strikes.length; strikeIdx += 1) {
        const index = strikeIdx * stride + minuteIdx
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
      const index = strikeIdx * stride + minuteIdx
      const out = strikeIdx * minutes + minuteIdx
      const callOtm = spot !== null && strike > spot ? raw.callVolume[index] : 0
      const putOtm = spot !== null && strike < spot ? raw.putVolume[index] : 0
      const callItm = spot !== null && strike <= spot ? raw.callVolume[index] : 0
      const putItm = spot !== null && strike >= spot ? raw.putVolume[index] : 0

      if (mode === 'oi') {
        call[out] = callOi[index]
        put[out] = putOi[index]
      } else if (mode === 'vol_otm') {
        call[out] = callOtm
        put[out] = putOtm
      } else if (mode === 'vol_itm') {
        call[out] = callItm
        put[out] = putItm
      } else if (mode === 'vol_signed') {
        signed[out] = raw.callVolume[index] - raw.putVolume[index]
      } else if (mode === 'oi_plus_otm') {
        const blend = (oi: number, otm: number): number =>
          OI_WEIGHT * (maxOi > 0 ? oi / maxOi : 0) + VOL_WEIGHT * (maxOtm > 0 ? otm / maxOtm : 0)
        call[out] = blend(callOi[index], callOtm)
        put[out] = blend(putOi[index], putOtm)
      } else if (mode === 'oi_minus_itm') {
        call[out] = callOi[index] - callItm
        put[out] = putOi[index] - putItm
      } else if (mode === 'vex') {
        call[out] = callVega[index] * raw.callOi[index]
        put[out] = putVega[index] * raw.putOi[index]
      } else if (mode === 'vex_signed') {
        signed[out] = callVega[index] * raw.callOi[index] - putVega[index] * raw.putOi[index]
      } else {
        signed[out] = callOi[index] - putOi[index]
      }
    }
  }

  // Grid nese staleAge kompaktně (index = strikeIdx * minutes + minuteIdx,
  // viz grid.ts) — s kapacitním stride je nutná přeskládaná kopie
  const compactStaleAge = ((): Float32Array | null => {
    if (!raw.staleAge) return null
    if (stride === minutes) return raw.staleAge
    const compact = new Float32Array(size)
    for (let strikeIdx = 0; strikeIdx < strikes.length; strikeIdx += 1) {
      const src = strikeIdx * stride
      compact.set(raw.staleAge.subarray(src, src + minutes), strikeIdx * minutes)
    }
    return compact
  })()

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
      staleAge: compactStaleAge,
    }
  }
  const signedScaled = transform(signed)
  const denominator = Math.max(p99Denominator(signedScaled), 1e-9)
  return {
    minutes,
    strikes,
    layers: { signed: normalize(signedScaled, denominator, -1) },
    staleAge: compactStaleAge,
  }
}
