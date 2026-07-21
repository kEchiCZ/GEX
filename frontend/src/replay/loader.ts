/** Načtení denního replay balíku (SPEC kap. 6 /replay) a příprava dat v paměti.

Balík se stahuje při načtení; přetáčení dne je pak čisté krájení polí v paměti
(AC issue #27: žádný fetch per frame). Snapshot matice chodí jako base64
Arrow IPC stream — dekóduje ji apache-arrow.

Živý provoz (#127): místo přenačítání celého balíku každou minutu se z WS kanálů
připojuje JEN nová minuta — `decodeBundle` → `ReplayInputs`, `appendMinute` přidá
minutu, `assembleReplayDay` z inputs poskládá `ReplayDay`. `buildReplayDay` je
složení obou (drží zpětně kompatibilní API i testy).
*/
import { tableFromIPC } from 'apache-arrow'
import { API_BASE } from '../config'
import type { PanelSeries } from '../components/BottomPanels'
import type { HeatmapGrid } from '../heatmap/grid'
import { buildModeGrid } from '../heatmap/modes'
import type { RawDay } from '../heatmap/modes'
import { maxPainSeries } from '../heatmap/maxpain'
import type { LevelLine, OverlayData, PriceBar } from '../heatmap/overlays'
import type { ProfileRow } from '../profile/bars'

/** Profilové řádky per minuta počítané LÍNĚ (#142).

Zobrazuje se vždy jen jedna minuta, ale předpočítání celého dne alokovalo
`minuty × strikes` objektů při každém appendu — po `maxPain` a popiscích os to byl
největší zbylý náklad uzavření minuty (a držel desítky MB). Řádky se proto počítají
na vyžádání a cachují per minuta. */
export interface ProfileSource {
  readonly length: number
  /** Řádky dané minuty; mimo rozsah prázdné pole. */
  rowsAt(minuteIdx: number): ProfileRow[]
}

/** Obalí hotová data (Daily pohled, testy) do `ProfileSource`. */
export function profileSourceOf(rows: ProfileRow[][]): ProfileSource {
  return { length: rows.length, rowsAt: (minuteIdx) => rows[minuteIdx] ?? [] }
}

export interface ReplayDay {
  symbol: string
  expiry: string
  date: string
  minutes: string[] // ISO timestampy minut (osa X)
  grid: HeatmapGrid // celý den (výchozí OI mód, normalizace p99)
  /** Surová snapshot matice — přepínání módů/škál lokálně (SPEC 4.3). */
  raw: RawDay
  overlays: OverlayData // celý den
  panels: PanelSeries // celý den
  /** Profilové řádky per minuta (líné — krájení bez přepočtu celého dne). */
  profileByMinute: ProfileSource
}

const LEVEL_KEYS = ['flip', 'centroid', 'call_wall', 'put_wall'] as const

interface BarInput {
  tsIso: string
  open?: number
  high?: number
  low?: number
  close: number
  volume: number
}
interface LevelsInput {
  tsIso: string
  values: Record<string, number | null>
}
interface FlowInput {
  tsIso: string
  cum_delta: number
}
interface OiPrevInput {
  strike: number
  right: string
  oi: number
}

/** Rozložený vstup dne — matice per-strike + řádky barů/levels/flow. Roste přes append. */
export interface ReplayInputs {
  symbol: string
  expiry: string
  date: string
  minutes: string[]
  strikes: number[]
  callOi: Float32Array
  putOi: Float32Array
  callVolume: Float32Array
  putVolume: Float32Array
  callDelta: Float32Array
  putDelta: Float32Array
  staleAge: Float32Array
  bars: BarInput[]
  levels: LevelsInput[]
  flow: FlowInput[]
  oiPrev: OiPrevInput[]
}

/** Jedna živá minuta z WS kanálů (#127) — snapshot řez + volitelně bar/levels/flow. */
export interface LiveMinuteRow {
  strike: number
  right: 'C' | 'P'
  oi: number
  volume: number
  delta: number
  stale_age?: number
}
export interface LiveMinute {
  tsIso: string
  rows: LiveMinuteRow[]
  bar?: { open?: number; high?: number; low?: number; close: number; volume?: number }
  levels?: Record<string, number | null>
  flow?: { cum_delta: number }
}

interface ReplayBundle {
  symbol: string
  expiry: string
  date: string
  snapshots_arrow_base64: string
  levels: Array<Record<string, unknown>>
  flow: Array<Record<string, unknown>>
  bars: Array<Record<string, unknown>>
  /** OI téže expirace z předchozího archivovaného dne (ΔOI vs. včera). */
  oi_prev?: Array<{ strike: number; right: string; oi: number }>
}

export interface DayListing {
  date: string
  expiry: string
}

/** Seznam uložených dnů instrumentu (pro Daily pohled) — s expirací per den. */
export async function fetchDays(symbol: string): Promise<DayListing[]> {
  const response = await fetch(`${API_BASE}/instruments/${symbol}/days`)
  if (!response.ok) {
    throw new Error(`Seznam dnů ${symbol} selhal: HTTP ${response.status}`)
  }
  const payload = (await response.json()) as { days: DayListing[] }
  return payload.days
}

async function fetchBundle(symbol: string, expiry: string, date: string): Promise<ReplayBundle> {
  const response = await fetch(`${API_BASE}/replay/${symbol}/${expiry}/${date}`)
  if (!response.ok) {
    throw new Error(`Replay ${symbol}/${expiry}/${date} selhal: HTTP ${response.status}`)
  }
  return (await response.json()) as ReplayBundle
}

export async function fetchReplay(
  symbol: string,
  expiry: string,
  date: string,
): Promise<ReplayDay> {
  return assembleReplayDay(decodeBundle(await fetchBundle(symbol, expiry, date)))
}

/** Rozložený vstup dne z /replay (pro živý append). */
export async function fetchReplayInputs(
  symbol: string,
  expiry: string,
  date: string,
): Promise<ReplayInputs> {
  return decodeBundle(await fetchBundle(symbol, expiry, date))
}

/** Kanonický klíč minuty: Arrow vrací timestamp jako epoch (ms), JSON jako ISO string. */
function canonicalTs(value: unknown): string {
  if (typeof value === 'number' || typeof value === 'bigint') {
    return new Date(Number(value)).toISOString()
  }
  if (value instanceof Date) return value.toISOString()
  const text = String(value)
  const asNumber = Number(text)
  if (Number.isFinite(asNumber) && !text.includes('-')) {
    return new Date(asNumber).toISOString()
  }
  const parsed = new Date(text)
  return Number.isNaN(parsed.getTime()) ? text : parsed.toISOString()
}

function base64ToBytes(base64: string): Uint8Array {
  const binary = atob(base64)
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index)
  }
  return bytes
}

function numOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** Dekóduje /replay balík na rozložený vstup (Arrow matice + řádky barů/levels/flow). */
export function decodeBundle(bundle: ReplayBundle): ReplayInputs {
  const table = tableFromIPC(base64ToBytes(bundle.snapshots_arrow_base64))
  const tsColumn = table.getChild('ts_min')
  const strikeColumn = table.getChild('strike')
  const rightColumn = table.getChild('right')
  const volumeColumn = table.getChild('volume')
  const oiColumn = table.getChild('oi')
  const deltaColumn = table.getChild('delta')
  const staleColumn = table.getChild('stale_age')
  if (!tsColumn || !strikeColumn || !rightColumn) {
    throw new Error('Replay balík: snapshot tabulka nemá očekávané sloupce')
  }

  const minuteKeys: string[] = []
  const minuteIndex = new Map<string, number>()
  const strikeSet = new Set<number>()
  const rowCount = table.numRows
  for (let row = 0; row < rowCount; row += 1) {
    const ts = canonicalTs(tsColumn.get(row))
    if (!minuteIndex.has(ts)) {
      minuteIndex.set(ts, minuteKeys.length)
      minuteKeys.push(ts)
    }
    strikeSet.add(Number(strikeColumn.get(row)))
  }
  const strikes = [...strikeSet].sort((a, b) => a - b)
  const strikeIndex = new Map(strikes.map((strike, index) => [strike, index]))
  const minutes = minuteKeys.length
  const size = minutes * strikes.length

  const callOi = new Float32Array(size)
  const putOi = new Float32Array(size)
  const callVolume = new Float32Array(size)
  const putVolume = new Float32Array(size)
  const callDelta = new Float32Array(size)
  const putDelta = new Float32Array(size)
  const staleAge = new Float32Array(size)

  for (let row = 0; row < rowCount; row += 1) {
    const minuteIdx = minuteIndex.get(canonicalTs(tsColumn.get(row)))!
    const strikeIdx = strikeIndex.get(Number(strikeColumn.get(row)))!
    const index = strikeIdx * minutes + minuteIdx
    const right = String(rightColumn.get(row)) as 'C' | 'P'
    const oi = Number(oiColumn?.get(row) ?? 0) || 0
    const volume = Number(volumeColumn?.get(row) ?? 0) || 0
    const delta = Number(deltaColumn?.get(row) ?? 0) || 0
    if (right === 'C') {
      callOi[index] = oi
      callVolume[index] = volume
      callDelta[index] = delta
    } else {
      putOi[index] = oi
      putVolume[index] = volume
      putDelta[index] = delta
    }
    staleAge[index] = Math.max(staleAge[index], Number(staleColumn?.get(row) ?? 0) || 0)
  }

  const bars: BarInput[] = bundle.bars.map((bar) => {
    const open = Number(bar.open)
    const high = Number(bar.high)
    const low = Number(bar.low)
    return {
      tsIso: canonicalTs(bar.ts_min),
      close: Number(bar.close),
      volume: Number(bar.volume) || 0,
      open: Number.isFinite(open) ? open : undefined,
      high: Number.isFinite(high) ? high : undefined,
      low: Number.isFinite(low) ? low : undefined,
    }
  })
  const levels: LevelsInput[] = bundle.levels.map((row) => ({
    tsIso: canonicalTs(row.ts_min),
    values: Object.fromEntries(LEVEL_KEYS.map((key) => [key, numOrNull(row[key])])),
  }))
  const flow: FlowInput[] = bundle.flow.map((row) => ({
    tsIso: canonicalTs(row.ts_min),
    cum_delta: Number(row.cum_delta) || 0,
  }))

  return {
    symbol: bundle.symbol,
    expiry: bundle.expiry,
    date: bundle.date,
    minutes: minuteKeys,
    strikes,
    callOi,
    putOi,
    callVolume,
    putVolume,
    callDelta,
    putDelta,
    staleAge,
    bars,
    levels,
    flow,
    oiPrev: (bundle.oi_prev ?? []).map((row) => ({
      strike: Number(row.strike),
      right: String(row.right),
      oi: Number(row.oi) || 0,
    })),
  }
}

function upsertRow<T extends { tsIso: string }>(rows: T[], row: T): T[] {
  const idx = rows.findIndex((existing) => existing.tsIso === row.tsIso)
  if (idx === -1) return [...rows, row]
  const copy = rows.slice()
  copy[idx] = row
  return copy
}

/** Připojí (nebo přepíše poslední) minutu do rozloženého vstupu — realokuje matice. */
export function appendMinute(inputs: ReplayInputs, minute: LiveMinute): ReplayInputs {
  const tsIso = canonicalTs(minute.tsIso)
  const existingIdx = inputs.minutes.indexOf(tsIso)
  const isAppend = existingIdx === -1
  const oldMinutes = inputs.minutes.length
  const newMinutes = isAppend ? [...inputs.minutes, tsIso] : inputs.minutes
  const newMinuteCount = newMinutes.length
  const targetMinute = isAppend ? oldMinutes : existingIdx

  const strikeSet = new Set(inputs.strikes)
  for (const row of minute.rows) strikeSet.add(row.strike)
  const strikesChanged = strikeSet.size !== inputs.strikes.length
  const newStrikes = strikesChanged ? [...strikeSet].sort((a, b) => a - b) : inputs.strikes
  const strikeCount = newStrikes.length
  const newStrikeIndex = new Map(newStrikes.map((strike, index) => [strike, index]))

  const size = strikeCount * newMinuteCount
  const callOi = new Float32Array(size)
  const putOi = new Float32Array(size)
  const callVolume = new Float32Array(size)
  const putVolume = new Float32Array(size)
  const callDelta = new Float32Array(size)
  const putDelta = new Float32Array(size)
  const staleAge = new Float32Array(size)

  // Přenos starých buněk na nový stride (minutes je násobitel řádku, viz grid.ts)
  for (let oldStrikeIdx = 0; oldStrikeIdx < inputs.strikes.length; oldStrikeIdx += 1) {
    const newStrikeIdx = newStrikeIndex.get(inputs.strikes[oldStrikeIdx])!
    for (let minuteIdx = 0; minuteIdx < oldMinutes; minuteIdx += 1) {
      const from = oldStrikeIdx * oldMinutes + minuteIdx
      const to = newStrikeIdx * newMinuteCount + minuteIdx
      callOi[to] = inputs.callOi[from]
      putOi[to] = inputs.putOi[from]
      callVolume[to] = inputs.callVolume[from]
      putVolume[to] = inputs.putVolume[from]
      callDelta[to] = inputs.callDelta[from]
      putDelta[to] = inputs.putDelta[from]
      staleAge[to] = inputs.staleAge[from]
    }
  }
  // Nová minuta ze snapshot řezu
  for (const row of minute.rows) {
    const to = newStrikeIndex.get(row.strike)! * newMinuteCount + targetMinute
    if (row.right === 'C') {
      callOi[to] = row.oi
      callVolume[to] = row.volume
      callDelta[to] = row.delta
    } else {
      putOi[to] = row.oi
      putVolume[to] = row.volume
      putDelta[to] = row.delta
    }
    staleAge[to] = Math.max(staleAge[to], row.stale_age ?? 0)
  }

  const bars = minute.bar
    ? upsertRow(inputs.bars, { tsIso, ...minute.bar, volume: minute.bar.volume ?? 0 })
    : inputs.bars
  const levels = minute.levels
    ? upsertRow(inputs.levels, {
        tsIso,
        values: Object.fromEntries(LEVEL_KEYS.map((key) => [key, minute.levels?.[key] ?? null])),
      })
    : inputs.levels
  const flow = minute.flow
    ? upsertRow(inputs.flow, { tsIso, cum_delta: minute.flow.cum_delta })
    : inputs.flow

  return {
    ...inputs,
    minutes: newMinutes,
    strikes: newStrikes,
    callOi,
    putOi,
    callVolume,
    putVolume,
    callDelta,
    putDelta,
    staleAge,
    bars,
    levels,
    flow,
  }
}

/** Poskládá `ReplayDay` (grid/overlays/panels/profil) z rozloženého vstupu. */
export function assembleReplayDay(inputs: ReplayInputs): ReplayDay {
  const { strikes } = inputs
  const minuteKeys = inputs.minutes
  const minutes = minuteKeys.length
  const minuteIndex = new Map(minuteKeys.map((ts, index) => [ts, index]))

  // Overlaye: cena z barů
  const price: PriceBar[] = []
  let previousClose = Number.NaN
  for (const bar of inputs.bars) {
    const minuteIdx = minuteIndex.get(bar.tsIso)
    if (minuteIdx !== undefined && Number.isFinite(bar.close)) {
      price.push({
        minuteIdx,
        close: bar.close,
        up: !(bar.close < previousClose),
        open: bar.open,
        high: bar.high,
        low: bar.low,
      })
      previousClose = bar.close
    }
  }
  const levelSeries = (key: string): (number | null)[] => {
    const series: (number | null)[] = Array.from({ length: minutes }, () => null)
    for (const row of inputs.levels) {
      const minuteIdx = minuteIndex.get(row.tsIso)
      if (minuteIdx !== undefined) series[minuteIdx] = row.values[key] ?? null
    }
    return series
  }

  const spotSeries: (number | null)[] = Array.from({ length: minutes }, () => null)
  for (const bar of price) spotSeries[bar.minuteIdx] = bar.close
  const raw: RawDay = {
    minutes,
    strikes,
    callOi: inputs.callOi,
    putOi: inputs.putOi,
    callVolume: inputs.callVolume,
    putVolume: inputs.putVolume,
    spotSeries,
    staleAge: inputs.staleAge,
  }
  const grid = buildModeGrid(raw, 'oi', 'linear')

  const levels: LevelLine[] = [
    { name: 'flip', color: '#e8c14b', series: levelSeries('flip') },
    { name: 'centroid', color: '#9d7be8', series: levelSeries('centroid') },
    { name: 'max_pain', color: '#d24bd2', series: maxPainSeries(raw) },
  ]
  const walls: LevelLine[] = [
    { name: 'call_wall', color: '#3ecf8e', series: levelSeries('call_wall') },
    { name: 'put_wall', color: '#f0616d', series: levelSeries('put_wall') },
  ]
  const overlays: OverlayData = {
    price,
    levels,
    walls,
    sessions: [],
    timestamp: minuteKeys.at(-1) ?? inputs.date,
  }

  const vol = Array.from({ length: minutes }, () => 0)
  for (const bar of inputs.bars) {
    const minuteIdx = minuteIndex.get(bar.tsIso)
    if (minuteIdx !== undefined) vol[minuteIdx] = bar.volume
  }
  const optVolCall = optVolSeries(inputs.callVolume, minutes, strikes.length)
  const optVolPut = optVolSeries(inputs.putVolume, minutes, strikes.length)
  const deltaFlowCall = deltaFlowSeries(
    inputs.callVolume,
    inputs.callDelta,
    minutes,
    strikes.length,
  )
  const deltaFlowPut = deltaFlowSeries(inputs.putVolume, inputs.putDelta, minutes, strikes.length)
  const cumDelta = Array.from({ length: minutes }, () => 0)
  for (const row of inputs.flow) {
    const minuteIdx = minuteIndex.get(row.tsIso)
    if (minuteIdx !== undefined) cumDelta[minuteIdx] = row.cum_delta
  }

  // ΔOI vs. předchozí archivovaný den téže expirace (null = není srovnání)
  const prevOi = new Map<string, number>()
  for (const row of inputs.oiPrev) prevOi.set(`${row.strike}|${row.right}`, row.oi)
  const totalOiToday =
    inputs.callOi.reduce((sum, value) => sum + value, 0) +
    inputs.putOi.reduce((sum, value) => sum + value, 0)
  const oiChangeReady = prevOi.size > 0 && totalOiToday > 0

  // Řádky profilu se počítají až při dotazu na konkrétní minutu a cachují se (#142)
  const profileCache = new Map<number, ProfileRow[]>()
  const profileByMinute: ProfileSource = {
    length: minutes,
    rowsAt(minuteIdx: number): ProfileRow[] {
      if (minuteIdx < 0 || minuteIdx >= minutes) return []
      const cached = profileCache.get(minuteIdx)
      if (cached) return cached
      const spotAtMinute = price.find((bar) => bar.minuteIdx === minuteIdx)?.close ?? Number.NaN
      const rows = strikes.map((strike, strikeIdx) => {
        const index = strikeIdx * minutes + minuteIdx
        const callAbsDelta = Math.abs(inputs.callDelta[index])
        const putAbsDelta = Math.abs(inputs.putDelta[index])
        return {
          strike,
          callVolComponent: inputs.callVolume[index] * callAbsDelta,
          callOiComponent: inputs.callOi[index] * callAbsDelta,
          putVolComponent: inputs.putVolume[index] * putAbsDelta,
          putOiComponent: inputs.putOi[index] * putAbsDelta,
          callVolume: inputs.callVolume[index],
          putVolume: inputs.putVolume[index],
          callOi: inputs.callOi[index],
          putOi: inputs.putOi[index],
          distanceFromSpot: Number.isFinite(spotAtMinute) ? strike - spotAtMinute : 0,
          callOiChange: oiChangeReady
            ? inputs.callOi[index] - (prevOi.get(`${strike}|C`) ?? 0)
            : null,
          putOiChange: oiChangeReady
            ? inputs.putOi[index] - (prevOi.get(`${strike}|P`) ?? 0)
            : null,
        }
      })
      profileCache.set(minuteIdx, rows)
      return rows
    },
  }

  return {
    symbol: inputs.symbol,
    expiry: inputs.expiry,
    date: inputs.date,
    minutes: minuteKeys,
    grid,
    raw,
    overlays,
    panels: { vol, optVolCall, optVolPut, cumDelta, deltaFlowCall, deltaFlowPut },
    profileByMinute,
  }
}

/** Sestaví den v paměti z /replay balíku (exportováno kvůli testům). */
export function buildReplayDay(bundle: ReplayBundle): ReplayDay {
  return assembleReplayDay(decodeBundle(bundle))
}

/** Δ Flow per minuta: Σ přes strikes |delta| × kladný přírůstek volume (SPEC 4.6 váhy). */
function deltaFlowSeries(
  volume: Float32Array,
  delta: Float32Array,
  minutes: number,
  strikeCount: number,
): number[] {
  const series = Array.from({ length: minutes }, () => 0)
  for (let strikeIdx = 0; strikeIdx < strikeCount; strikeIdx += 1) {
    for (let minuteIdx = 1; minuteIdx < minutes; minuteIdx += 1) {
      const index = strikeIdx * minutes + minuteIdx
      const increment = volume[index] - volume[index - 1]
      if (increment > 0) series[minuteIdx] += increment * Math.abs(delta[index])
    }
  }
  return series
}

/** OptVol per minuta: Σ kladných přírůstků kumulativního volume přes strikes. */
function optVolSeries(volume: Float32Array, minutes: number, strikeCount: number): number[] {
  const series = Array.from({ length: minutes }, () => 0)
  for (let strikeIdx = 0; strikeIdx < strikeCount; strikeIdx += 1) {
    for (let minuteIdx = 1; minuteIdx < minutes; minuteIdx += 1) {
      const index = strikeIdx * minutes + minuteIdx
      const increment = volume[index] - volume[index - 1]
      if (increment > 0) series[minuteIdx] += increment
    }
  }
  return series
}
