/** Snímek GEX kontextu k okamžiku záznamu deníku (#711).

Proč vůbec: poziční mapa se během dne mění. Bez uloženého snímku V ČASE
VSTUPU nelze po měsíci rozlišit „setup nefunguje" od „vzal jsem ho
v podmínkách, ve kterých platit nemohl".

Proč se ukládá HODNOTA, ne odkaz na přepočet:
1. retence — Parquet partice mají 90 dní (ADR-0022), deník je trvalý;
2. mechanika se verzuje (SETUP_MECHANICS_VERSION), takže zpětný přepočet by
   dal jiná čísla než ta, podle kterých se rozhodovalo.

Chybějící zdroj dává `null`. NIKDY se nedosazuje nula ani se pole tiše
nevynechá — nula je platná cena a tichý výpadek by se tvářil jako měření.
*/
import type { BarRow, CliffToday, LevelsRow, RangeSummary } from '../api/briefing'
import {
  barsRange,
  fetchBars,
  fetchCliffToday,
  fetchLevelsSeries,
  fetchStoredDays,
  gammaRegimeLabel,
  previousStoredDay,
} from '../api/briefing'
import { fetchTendency } from '../api/tendency'
import type { TendencyRow } from '../api/tendency'

/** Verze schématu kontextu — roste, když se změní význam polí. */
export const CONTEXT_VERSION = 1

export interface JournalContextLevel {
  name: string
  price: number
  /** Kladné = úroveň je nad cenou. */
  distance: number
}

export interface JournalContext {
  version: number
  ts_ref: string
  symbol: string
  expiry: string | null
  regime: string | null
  total_gex: number | null
  flip: number | null
  call_wall: number | null
  put_wall: number | null
  centroid: number | null
  spot: number | null
  dist_to_flip: number | null
  nearest_level: JournalContextLevel | null
  session: RangeSummary | null
  prev_day: RangeSummary | null
  cliff_share: number | null
  is_opex: boolean | null
  tendency_score: number | null
  tendency_band: string | null
}

export interface ContextInputs {
  symbol: string
  expiry: string | null
  tsRef: string
  levels: LevelsRow[]
  bars: BarRow[]
  prevDayBars: BarRow[]
  cliff: CliffToday | null
  tendency: TendencyRow[]
}

/** Řádek s časem nejbližším `targetMs`, nebo null když řada nic nemá.
 *
 * Vybírá se NEJBLIŽŠÍ, ne poslední — záznam se často zapisuje zpětně
 * (Shift+klik do historie) a poslední řádek dne by popisoval jinou minutu.
 */
function nearestByTime<T extends { ts_min: string }>(rows: T[], targetMs: number): T | null {
  let best: T | null = null
  let bestDelta = Number.POSITIVE_INFINITY
  for (const row of rows) {
    const delta = Math.abs(Date.parse(row.ts_min) - targetMs)
    if (delta < bestDelta) {
      best = row
      bestDelta = delta
    }
  }
  return best
}

/** Nejbližší pojmenovaná úroveň k ceně — „o čem ten obchod vlastně byl". */
export function nearestLevel(
  levels: LevelsRow | null,
  spot: number | null,
): JournalContextLevel | null {
  if (levels === null || spot === null) return null
  const candidates: Array<[string, number | null]> = [
    ['flip', levels.flip],
    ['call_wall', levels.call_wall],
    ['put_wall', levels.put_wall],
    ['centroid', levels.centroid],
  ]
  let best: JournalContextLevel | null = null
  for (const [name, price] of candidates) {
    if (price === null) continue
    const distance = price - spot
    if (best === null || Math.abs(distance) < Math.abs(best.distance)) {
      best = { name, price, distance }
    }
  }
  return best
}

/** Poskládá snímek kontextu. Čistá funkce — žádné fetche, žádný čas. */
export function composeContext(input: ContextInputs): JournalContext {
  const targetMs = Date.parse(input.tsRef)
  const levelsRow = Number.isFinite(targetMs) ? nearestByTime(input.levels, targetMs) : null
  const bar = Number.isFinite(targetMs) ? nearestByTime(input.bars, targetMs) : null
  const tendencyRow = Number.isFinite(targetMs) ? nearestByTime(input.tendency, targetMs) : null
  const spot = bar?.close ?? null

  return {
    version: CONTEXT_VERSION,
    ts_ref: input.tsRef,
    symbol: input.symbol,
    expiry: input.expiry,
    regime: levelsRow ? gammaRegimeLabel(levelsRow, spot) : null,
    total_gex: levelsRow?.total_gex ?? null,
    flip: levelsRow?.flip ?? null,
    call_wall: levelsRow?.call_wall ?? null,
    put_wall: levelsRow?.put_wall ?? null,
    centroid: levelsRow?.centroid ?? null,
    spot,
    dist_to_flip:
      spot !== null && levelsRow?.flip != null ? Number((spot - levelsRow.flip).toFixed(4)) : null,
    nearest_level: nearestLevel(levelsRow, spot),
    // Rozsah seance jen DO okamžiku záznamu — pozdější extrémy jsem tehdy
    // nemohl vidět a v retrospektivě by tvořily falešnou jistotu.
    session: Number.isFinite(targetMs) ? barsRange(input.bars, targetMs) : null,
    prev_day: barsRange(input.prevDayBars),
    cliff_share: input.cliff?.cliff_share ?? null,
    is_opex: input.cliff?.is_opex ?? null,
    tendency_score: tendencyRow?.score ?? null,
    tendency_band: tendencyRow?.band ?? null,
  }
}

/**
 * Načte podklady a poskládá kontext k `tsRef`.
 *
 * JEDINÁ cesta ke snímku — zápis z heatmapy i z obrazovky Deník volají tuhle
 * funkci, takže shodný vstup dá shodný výstup bez druhé implementace, kterou
 * by bylo nutné držet v paritě.
 */
export async function loadJournalContext(input: {
  symbol: string
  expiry: string | null
  tsRef: string
}): Promise<JournalContext> {
  const dayIso = input.tsRef.slice(0, 10)
  const [levels, bars, cliff, tendency, days] = await Promise.all([
    input.expiry ? fetchLevelsSeries(input.symbol, input.expiry, dayIso) : Promise.resolve([]),
    fetchBars(input.symbol, dayIso),
    fetchCliffToday(input.symbol),
    fetchTendency(input.symbol, dayIso),
    fetchStoredDays(input.symbol),
  ])
  const prevDay = previousStoredDay(days, dayIso)
  const prevDayBars = prevDay ? await fetchBars(input.symbol, prevDay) : []
  return composeContext({
    symbol: input.symbol,
    expiry: input.expiry,
    tsRef: input.tsRef,
    levels,
    bars,
    prevDayBars,
    cliff,
    tendency,
  })
}

/** Popisky polí pro čitelný detail — surové JSON uživateli nic neřekne. */
export const CONTEXT_LABELS: Record<string, string> = {
  regime: 'Režim',
  total_gex: 'Net GEX',
  flip: 'Flip',
  call_wall: 'Call wall',
  put_wall: 'Put wall',
  centroid: 'Centroid',
  spot: 'Cena',
  dist_to_flip: 'Vzdálenost k flipu',
  cliff_share: 'Odpad gammy',
  tendency_score: 'Tendence',
  tendency_band: 'Pásmo tendence',
}
