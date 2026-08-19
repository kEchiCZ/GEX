/** Svíčky přes hranici dne (#788) — historie z věčného archivu barů.

Graf je ukotvený na DNEŠKU: koš 0 = první koš dnešní seance, historie dostává
ZÁPORNÉ indexy košů a roste jen doleva. Díky kotvě napravo se při dotažení
staršího dne nic neposouvá — pixelové pozice stávajících svíček drží bez
kompenzace offsetu (mřížka `HeatmapGrid` se historie vůbec nedotkne).

Dny se skládají zády k sobě po obchodních minutách — víkend ani denní pauza
CME nevyrábí prázdnou mezeru (přeskočí se, stejně jako je dnešní osa jen
seanční). Každý den je samostatný „slice": kreslí se zvlášť, takže cenová
křivka NEspojuje hranici dnů — přes roll kontraktu (~2. čtvrtek měsíce
expirace) by spojnice tvrdila falešnou kontinuitu (ADR-0028 dodatek).

Jen cena. Heatmapa, panely ani profily se pro minulé dny nestaví (rozhodnutí
zadavatele v #788 — positioning per den stojí na jiném 0DTE řetězu).
*/

import { API_BASE } from '../config'
import type { PriceBar } from '../heatmap/overlays'
import { cachedBucketPlan } from '../heatmap/buckets'
import { aggregateBars } from './aggregate'

/** Jedna historická seance: 1m bary s `minuteIdx` = pořadí v seanci (0..N−1). */
export interface HistoryDay {
  date: string
  minutesIso: string[]
  bars: PriceBar[]
}

/** Den připravený ke kreslení: koše se ZÁPORNÝMI indexy (kotva = dnešek). */
export interface HistorySlice {
  date: string
  /** První (nejlevější) koš slice — sem patří svislý předěl s datem. */
  firstBucket: number
  price: PriceBar[]
}

export interface HistoryView {
  /** Od nejstaršího k nejnovějšímu; prázdné = žádná historie načtená. */
  slices: HistorySlice[]
  /** Nejlevější načtený koš (0 = nic); práh pro dotažení dalšího dne. */
  firstBucket: number
}

export const EMPTY_HISTORY: HistoryView = { slices: [], firstBucket: 0 }

interface BarsPayloadRow {
  ts_min: string
  open?: number | null
  high?: number | null
  low?: number | null
  close?: number | null
  volume?: number | null
}

/** Odpověď `GET /bars/{symbol}?date=` → HistoryDay; null = prázdná seance. */
export function parseHistoryDay(date: string, payload: unknown): HistoryDay | null {
  const rows = (payload as { bars?: BarsPayloadRow[] } | null)?.bars
  if (!Array.isArray(rows) || rows.length === 0) return null
  const sorted = rows
    .filter((row) => typeof row.ts_min === 'string' && typeof row.close === 'number')
    .sort((a, b) => a.ts_min.localeCompare(b.ts_min))
  if (sorted.length === 0) return null
  const minutesIso: string[] = []
  const bars: PriceBar[] = []
  let previousClose = Number.NaN
  for (const row of sorted) {
    const close = row.close as number
    minutesIso.push(row.ts_min)
    bars.push({
      minuteIdx: bars.length,
      close,
      open: row.open ?? undefined,
      high: row.high ?? undefined,
      low: row.low ?? undefined,
      up: Number.isNaN(previousClose) ? true : !(close < previousClose),
    })
    previousClose = close
  }
  return { date, minutesIso, bars }
}

/** Poskládá načtené dny (od nejnovějšího po nejstarší) do košů aktuálního TF.

Agregace jede stejným strojem jako dnešní osa (`cachedBucketPlan` +
`aggregateBars`, zarovnání košů na wall-clock #584) — 5m svíčka historie
vzniká stejně jako 5m svíčka dneška. Přepnutí TF celý pohled přepočítá
(vstup je pořád 1m), kotva na koši 0 zůstává.
*/
export function buildHistoryView(
  daysNewestFirst: HistoryDay[],
  bucketMinutes: number,
): HistoryView {
  if (daysNewestFirst.length === 0) return EMPTY_HISTORY
  const slices: HistorySlice[] = []
  let offset = 0 // pravá hrana příštího slice (exkluzivně); začíná na koši 0 = dnešek
  for (const day of daysNewestFirst) {
    const plan = cachedBucketPlan(day.minutesIso, day.minutesIso.length, bucketMinutes)
    const aggregated = bucketMinutes > 1 ? aggregateBars(day.bars, plan) : day.bars
    const buckets = Math.max(plan.buckets, 1)
    const firstBucket = offset - buckets
    slices.push({
      date: day.date,
      firstBucket,
      price: aggregated.map((bar) => ({ ...bar, minuteIdx: bar.minuteIdx + firstBucket })),
    })
    offset = firstBucket
  }
  slices.reverse() // kreslí se od nejstaršího — na pořadí nezáleží, ať je ale stabilní
  return { slices, firstBucket: offset }
}

/** Stáhne bary jedné seance; null = seance neexistuje (víkend/svátek → 404). */
export async function fetchHistoryDay(symbol: string, date: string): Promise<HistoryDay | null> {
  const response = await fetch(`${API_BASE}/bars/${symbol}?date=${date}`)
  if (response.status === 404) return null
  if (!response.ok) throw new Error(`bars ${date}: HTTP ${response.status}`)
  return parseHistoryDay(date, await response.json())
}

/** Předchozí kalendářní den ISO data (UTC aritmetika — žádné DST pasti). */
export function previousDateIso(dateIso: string): string {
  const ts = Date.parse(`${dateIso}T00:00:00Z`) - 24 * 3600 * 1000
  return new Date(ts).toISOString().slice(0, 10)
}
