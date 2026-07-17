/** Načtení denního replay balíku (SPEC kap. 6 /replay) a příprava dat v paměti.

Balík se stahuje JEDNOU; přetáčení dne je pak čisté krájení polí v paměti
(AC issue #27: žádný fetch per frame). Snapshot matice chodí jako base64
Arrow IPC stream — dekóduje ji apache-arrow.
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
  /** Profilové řádky per minuta (předpočítané — krájení bez přepočtu). */
  profileByMinute: ProfileRow[][]
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

export async function fetchReplay(
  symbol: string,
  expiry: string,
  date: string,
): Promise<ReplayDay> {
  const response = await fetch(`${API_BASE}/replay/${symbol}/${expiry}/${date}`)
  if (!response.ok) {
    throw new Error(`Replay ${symbol}/${expiry}/${date} selhal: HTTP ${response.status}`)
  }
  const bundle = (await response.json()) as ReplayBundle
  return buildReplayDay(bundle)
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

/** Sestaví den v paměti z /replay balíku (exportováno kvůli testům). */
export function buildReplayDay(bundle: ReplayBundle): ReplayDay {
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

  // Osy: minuty a strikes
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
  const staleAge = new Float32Array(size)
  const volumeByCell = { C: new Float32Array(size), P: new Float32Array(size) }
  const deltaByCell = { C: new Float32Array(size), P: new Float32Array(size) }

  for (let row = 0; row < rowCount; row += 1) {
    const minuteIdx = minuteIndex.get(canonicalTs(tsColumn.get(row)))!
    const strikeIdx = strikeIndex.get(Number(strikeColumn.get(row)))!
    const index = strikeIdx * minutes + minuteIdx
    const right = String(rightColumn.get(row)) as 'C' | 'P'
    const oi = Number(oiColumn?.get(row) ?? 0) || 0
    const volume = Number(volumeColumn?.get(row) ?? 0) || 0
    const delta = Number(deltaColumn?.get(row) ?? 0) || 0
    if (right === 'C') callOi[index] = oi
    else putOi[index] = oi
    volumeByCell[right][index] = volume
    deltaByCell[right][index] = delta
    staleAge[index] = Math.max(staleAge[index], Number(staleColumn?.get(row) ?? 0) || 0)
  }

  // Overlaye: cena z barů, levels z derived
  const price: PriceBar[] = []
  let previousClose = Number.NaN
  for (const bar of bundle.bars) {
    const ts = canonicalTs(bar.ts_min)
    const minuteIdx = minuteIndex.get(ts)
    const close = Number(bar.close)
    if (minuteIdx !== undefined && Number.isFinite(close)) {
      const open = Number(bar.open)
      const high = Number(bar.high)
      const low = Number(bar.low)
      price.push({
        minuteIdx,
        close,
        up: !(close < previousClose),
        open: Number.isFinite(open) ? open : undefined,
        high: Number.isFinite(high) ? high : undefined,
        low: Number.isFinite(low) ? low : undefined,
      })
      previousClose = close
    }
  }
  const levelSeries = (key: string): (number | null)[] => {
    const series: (number | null)[] = Array.from({ length: minutes }, () => null)
    for (const row of bundle.levels) {
      const minuteIdx = minuteIndex.get(canonicalTs(row.ts_min))
      if (minuteIdx === undefined) continue
      const value = row[key]
      series[minuteIdx] = typeof value === 'number' ? value : null
    }
    return series
  }
  // Surová matice + výchozí grid (OI mód; volume fallback řeší buildModeGrid)
  const spotSeries: (number | null)[] = Array.from({ length: minutes }, () => null)
  for (const bar of price) {
    spotSeries[bar.minuteIdx] = bar.close
  }
  const raw: RawDay = {
    minutes,
    strikes,
    callOi,
    putOi,
    callVolume: volumeByCell.C,
    putVolume: volumeByCell.P,
    spotSeries,
    staleAge,
  }
  const grid = buildModeGrid(raw, 'oi', 'linear')

  const levels: LevelLine[] = [
    { name: 'flip', color: '#e8c14b', series: levelSeries('flip') },
    { name: 'centroid', color: '#9d7be8', series: levelSeries('centroid') },
    // Max Pain z OI (klient-side; bez OI je řada null a linie se nekreslí)
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
    timestamp: minuteKeys.at(-1) ?? bundle.date,
  }

  // Spodní panely: Vol z barů, OptVol z minutových přírůstků, CumΔ z flow
  const vol = Array.from({ length: minutes }, () => 0)
  for (const bar of bundle.bars) {
    const minuteIdx = minuteIndex.get(canonicalTs(bar.ts_min))
    if (minuteIdx !== undefined) vol[minuteIdx] = Number(bar.volume) || 0
  }
  const optVolCall = optVolSeries(volumeByCell.C, minutes, strikes.length)
  const optVolPut = optVolSeries(volumeByCell.P, minutes, strikes.length)
  // Δ Flow: delta-vážený tok per strana — čtení „obchody na call/put straně" (Moodix)
  const deltaFlowCall = deltaFlowSeries(volumeByCell.C, deltaByCell.C, minutes, strikes.length)
  const deltaFlowPut = deltaFlowSeries(volumeByCell.P, deltaByCell.P, minutes, strikes.length)
  const cumDelta = Array.from({ length: minutes }, () => 0)
  for (const row of bundle.flow) {
    const minuteIdx = minuteIndex.get(canonicalTs(row.ts_min))
    if (minuteIdx !== undefined) cumDelta[minuteIdx] = Number(row.cum_delta) || 0
  }

  // ΔOI vs. předchozí archivovaný den téže expirace (null = není srovnání)
  const prevOi = new Map<string, number>()
  for (const row of bundle.oi_prev ?? []) {
    prevOi.set(`${row.strike}|${row.right}`, Number(row.oi) || 0)
  }
  const totalOiToday =
    callOi.reduce((sum, value) => sum + value, 0) + putOi.reduce((sum, value) => sum + value, 0)
  const oiChangeReady = prevOi.size > 0 && totalOiToday > 0

  // Strike profil per minuta (combined varianta, w=1 — SPEC 4.6)
  const profileByMinute: ProfileRow[][] = []
  for (let minuteIdx = 0; minuteIdx < minutes; minuteIdx += 1) {
    const spotAtMinute = price.find((bar) => bar.minuteIdx === minuteIdx)?.close ?? Number.NaN
    profileByMinute.push(
      strikes.map((strike, strikeIdx) => {
        const index = strikeIdx * minutes + minuteIdx
        const callAbsDelta = Math.abs(deltaByCell.C[index])
        const putAbsDelta = Math.abs(deltaByCell.P[index])
        return {
          strike,
          callVolComponent: volumeByCell.C[index] * callAbsDelta,
          callOiComponent: callOi[index] * callAbsDelta,
          putVolComponent: volumeByCell.P[index] * putAbsDelta,
          putOiComponent: putOi[index] * putAbsDelta,
          callVolume: volumeByCell.C[index],
          putVolume: volumeByCell.P[index],
          callOi: callOi[index],
          putOi: putOi[index],
          distanceFromSpot: Number.isFinite(spotAtMinute) ? strike - spotAtMinute : 0,
          callOiChange: oiChangeReady ? callOi[index] - (prevOi.get(`${strike}|C`) ?? 0) : null,
          putOiChange: oiChangeReady ? putOi[index] - (prevOi.get(`${strike}|P`) ?? 0) : null,
        }
      }),
    )
  }

  return {
    symbol: bundle.symbol,
    expiry: bundle.expiry,
    date: bundle.date,
    minutes: minuteKeys,
    grid,
    raw,
    overlays,
    panels: { vol, optVolCall, optVolPut, cumDelta, deltaFlowCall, deltaFlowPut },
    profileByMinute,
  }
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
