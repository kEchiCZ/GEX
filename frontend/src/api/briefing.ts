/** Ranní briefing (#674): REST klient + čisté skládací helpery.

Obrazovka je ČISTÁ KOMPOZICE existujících dat — žádný nový výpočet v enginu.
Fetchery čtou lehké endpointy (/bars, /oidelta, /levels, /gexforward,
/gammacliff, /news/upcoming, /sentiment/state); helpery z barů skládají
overnight rozsah a včerejší settle. US open se počítá DST-korektně přes
zonedTimeUtc (#511).
*/
import { API_BASE } from '../config'
import { zonedTimeUtc } from '../instrument/tz'

export interface BarRow {
  ts_min: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface LevelsRow {
  ts_min: string
  flip: number | null
  call_wall: number | null
  put_wall: number | null
  centroid: number | null
  total_gex: number
}

export interface OiDeltaSummary {
  symbol: string
  expiry: string
  days: { current: string; previous: string | null } | null
  call_total?: number
  put_total?: number
  call_delta?: number
  put_delta?: number
  movers?: Array<{ strike: number; right: 'C' | 'P'; oi: number; delta: number }>
}

export interface CliffToday {
  session_date: string
  cliff_share: number | null
  is_opex: boolean
}

async function getJson<T>(path: string, fallback: T): Promise<T> {
  try {
    const response = await fetch(`${API_BASE}${path}`)
    if (!response.ok) return fallback
    return (await response.json()) as T
  } catch {
    return fallback
  }
}

export async function fetchBars(symbol: string, dateIso: string): Promise<BarRow[]> {
  const data = await getJson<{ bars: BarRow[] }>(`/bars/${symbol}?date=${dateIso}`, { bars: [] })
  return data.bars
}

export async function fetchLevelsSeries(
  symbol: string,
  expiry: string,
  dateIso: string,
): Promise<LevelsRow[]> {
  const data = await getJson<{ levels: LevelsRow[] }>(
    `/levels/${symbol}/${expiry}?date=${dateIso}`,
    { levels: [] },
  )
  return data.levels
}

export async function fetchOiDelta(symbol: string, expiry: string): Promise<OiDeltaSummary> {
  return getJson<OiDeltaSummary>(`/oidelta/${symbol}/${expiry}`, {
    symbol,
    expiry,
    days: null,
  })
}

export async function fetchCliffToday(symbol: string): Promise<CliffToday | null> {
  const data = await getJson<{ today: CliffToday | null }>(`/gammacliff/${symbol}`, {
    today: null,
  })
  return data.today
}

export async function fetchStoredDays(symbol: string): Promise<string[]> {
  const data = await getJson<{ days: Array<{ date: string }> }>(`/instruments/${symbol}/days`, {
    days: [],
  })
  return data.days.map((day) => day.date)
}

/** US open (9:30 New York) daného dne v epoch ms — DST řeší zoneinfo (#511). */
export function usOpenMs(dateIso: string): number {
  const [year, month, day] = dateIso.split('-').map(Number)
  return zonedTimeUtc('America/New_York', year, month, day, 9, 30)
}

export interface RangeSummary {
  high: number
  low: number
  last: number
  lastTs: string
}

/** Extrémy a poslední close z barů; volitelně jen do okamžiku `untilMs`. */
export function barsRange(bars: BarRow[], untilMs?: number): RangeSummary | null {
  let summary: RangeSummary | null = null
  for (const bar of bars) {
    if (untilMs !== undefined && Date.parse(bar.ts_min) >= untilMs) continue
    if (summary === null) {
      summary = { high: bar.high, low: bar.low, last: bar.close, lastTs: bar.ts_min }
    } else {
      summary.high = Math.max(summary.high, bar.high)
      summary.low = Math.min(summary.low, bar.low)
      summary.last = bar.close
      summary.lastTs = bar.ts_min
    }
  }
  return summary
}

/** Poslední řádek levels řady — aktuální flip/walls/total_gex briefingu. */
export function latestLevels(rows: LevelsRow[]): LevelsRow | null {
  return rows.length > 0 ? rows[rows.length - 1] : null
}

/** Poslední uložený den PŘED `dateIso` — včerejší seance pro settle/PDH/PDL. */
export function previousStoredDay(days: string[], dateIso: string): string | null {
  const before = days.filter((day) => day < dateIso).sort()
  return before.length > 0 ? before[before.length - 1] : null
}

/** Režim gammy pro briefing: znaménko total_gex + poloha spotu vůči flipu. */
export function gammaRegimeLabel(levels: LevelsRow | null, spot: number | null): string {
  if (levels === null) return 'bez dat'
  const sign = levels.total_gex >= 0 ? 'pozitivní gamma (pohyb se tlumí)' : 'negativní gamma (pohyb se zesiluje)' // prettier-ignore
  if (spot === null || levels.flip === null) return sign
  const side = spot >= levels.flip ? 'nad flipem' : 'pod flipem'
  return `${sign}, cena ${side}`
}

/** Text ranního plánu do deníku (#673) — předvyplněná kostra z briefingu. */
export function briefingToPlanText(input: {
  symbol: string
  regime: string
  levels: LevelsRow | null
  overnight: RangeSummary | null
  prevDay: RangeSummary | null
  cliff: CliffToday | null
}): string {
  const lines: string[] = [`Plán dne ${input.symbol}:`, `- Režim: ${input.regime}`]
  const { levels } = input
  if (levels) {
    const fmt = (value: number | null) => (value === null ? '—' : String(value))
    lines.push(
      `- Úrovně: flip ${fmt(levels.flip)}, call wall ${fmt(levels.call_wall)}, put wall ${fmt(levels.put_wall)}`,
    )
  }
  if (input.prevDay) lines.push(`- Včera: settle ${input.prevDay.last}, rozsah ${input.prevDay.low}–${input.prevDay.high}`) // prettier-ignore
  if (input.overnight) lines.push(`- Overnight: ${input.overnight.low}–${input.overnight.high}, teď ${input.overnight.last}`) // prettier-ignore
  if (input.cliff?.cliff_share != null) {
    const pct = Math.round(input.cliff.cliff_share * 100)
    lines.push(`- Dnes odpadá ~${pct} % gammy${input.cliff.is_opex ? ' (OPEX!)' : ''}`)
  }
  lines.push('- Teze dne: ')
  return lines.join('\n')
}
