/** Statistiky deníku (#714) — čisté agregace nad záznamy.

Zásada, kterou drží celý repo (Wilson gate #453, track record ADR-0021):
**neukazovat statistiku, která stojí na příliš malém vzorku.** Pod prahem se
vrací počet vzorků a příznak `small`, ať UI napíše „málo dat" místo grafu,
který svádí k závěru.

Druhá zásada: co jde odvodit, se nepočítá podruhé. R se bere z `realizedR`,
body z `resultPoints` — stejné funkce, jaké používá formulář i export.
*/
import type { JournalEntry, JournalTrade } from '../api/journal'
import { realizedR } from '../api/journal'
import { resultPoints } from './futures'

/** Pod tímhle počtem uzavřených obchodů je každý závěr náhoda. */
export const MIN_SAMPLE = 20

export interface GroupStats {
  key: string
  /** Počet uzavřených obchodů ve skupině. */
  n: number
  wins: number
  winRate: number
  /** Průměrné R — očekávaná hodnota na jeden obchod. */
  expectancy: number
  /** Σ zisků / Σ ztrát (v R); null když nejsou ztráty. */
  profitFactor: number | null
  avgWin: number
  avgLoss: number
  sumR: number
  sumPoints: number
  /** true = vzorek je pod prahem, závěry se z něj dělat nemají. */
  small: boolean
}

/** Obchod je uzavřený, když z něj jde spočítat R. */
export function closedTrades(entries: JournalEntry[]): Array<{
  entry: JournalEntry
  trade: JournalTrade
  r: number
}> {
  const result: Array<{ entry: JournalEntry; trade: JournalTrade; r: number }> = []
  for (const entry of entries) {
    if (entry.entry_type !== 'obchod' || !entry.trade) continue
    const r = realizedR(entry.trade)
    if (r === null) continue
    result.push({ entry, trade: entry.trade, r })
  }
  return result
}

function statsFor(key: string, rows: Array<{ trade: JournalTrade; r: number }>): GroupStats {
  const wins = rows.filter((row) => row.r > 0)
  const losses = rows.filter((row) => row.r < 0)
  const sumWin = wins.reduce((total, row) => total + row.r, 0)
  const sumLoss = losses.reduce((total, row) => total + Math.abs(row.r), 0)
  const sumR = rows.reduce((total, row) => total + row.r, 0)
  const sumPoints = rows.reduce((total, row) => total + (resultPoints(row.trade) ?? 0), 0)
  return {
    key,
    n: rows.length,
    wins: wins.length,
    winRate: rows.length > 0 ? wins.length / rows.length : 0,
    expectancy: rows.length > 0 ? sumR / rows.length : 0,
    // Bez ztrát není profit factor definovaný — nekreslit „nekonečno"
    profitFactor: sumLoss > 0 ? sumWin / sumLoss : null,
    avgWin: wins.length > 0 ? sumWin / wins.length : 0,
    avgLoss: losses.length > 0 ? sumLoss / losses.length : 0,
    sumR,
    sumPoints,
    small: rows.length < MIN_SAMPLE,
  }
}

/**
 * Agregace podle libovolného klíče. Záznam bez klíče se vynechá —
 * skupina „—" by mísila „neměřeno" s reálnou kategorií.
 */
export function groupBy(
  entries: JournalEntry[],
  keyOf: (entry: JournalEntry, trade: JournalTrade) => string | null,
): GroupStats[] {
  const buckets = new Map<string, Array<{ trade: JournalTrade; r: number }>>()
  for (const row of closedTrades(entries)) {
    const key = keyOf(row.entry, row.trade)
    if (key === null || key === '') continue
    buckets.set(key, [...(buckets.get(key) ?? []), { trade: row.trade, r: row.r }])
  }
  return [...buckets.entries()].map(([key, rows]) => statsFor(key, rows)).sort((a, b) => b.n - a.n)
}

/** Kolik stojí která chyba — Σ P/L obchodů s daným tagem. */
export function mistakeCost(
  entries: JournalEntry[],
): Array<{ tag: string; n: number; pnl: number }> {
  const totals = new Map<string, { n: number; pnl: number }>()
  for (const entry of entries) {
    const trade = entry.trade
    if (!trade) continue
    for (const tag of trade.mistake_tags) {
      const current = totals.get(tag) ?? { n: 0, pnl: 0 }
      totals.set(tag, { n: current.n + 1, pnl: current.pnl + (trade.net_pnl ?? 0) })
    }
  }
  return [...totals.entries()]
    .map(([tag, value]) => ({ tag, ...value }))
    .sort((a, b) => a.pnl - b.pnl)
}

/** Histogram R do košů po 0,5 R — tvar rozdělení řekne víc než průměr. */
export function rHistogram(entries: JournalEntry[]): Array<{ bucket: number; count: number }> {
  const counts = new Map<number, number>()
  for (const row of closedTrades(entries)) {
    const bucket = Math.floor(row.r * 2) / 2
    counts.set(bucket, (counts.get(bucket) ?? 0) + 1)
  }
  return [...counts.entries()]
    .map(([bucket, count]) => ({ bucket, count }))
    .sort((a, b) => a.bucket - b.bucket)
}

/** Plánované vs. realizované R:R — odhalí systematický optimismus v plánu. */
export function plannedVsRealized(entries: JournalEntry[]): {
  n: number
  avgPlanned: number
  avgRealized: number
} | null {
  const rows = closedTrades(entries).filter(
    (row) =>
      row.trade.planned_entry !== null &&
      row.trade.planned_stop !== null &&
      row.trade.planned_target !== null,
  )
  if (rows.length === 0) return null
  const planned = rows.map((row) => {
    const { planned_entry: entry, planned_stop: stop, planned_target: target } = row.trade
    const risk = Math.abs((entry as number) - (stop as number))
    return risk > 0 ? Math.abs((target as number) - (entry as number)) / risk : 0
  })
  return {
    n: rows.length,
    avgPlanned: planned.reduce((total, value) => total + value, 0) / rows.length,
    avgRealized: rows.reduce((total, row) => total + row.r, 0) / rows.length,
  }
}

export interface DetectorComparison {
  /** Detektor nabídl a vzal jsem ho. */
  taken: number
  /** Detektor nabídl a přeskočil jsem ho — cena váhavosti (#715). */
  skipped: number
  /** Vzal jsem bez nabídky detektoru — vlastní edge, nebo improvizace? */
  own: number
}

/**
 * Srovnání detektoru s realitou (#627).
 *
 * `setup_id` = záznam vznikl u detekovaného setupu. Obchod s ním = vzal jsem
 * nabídku; typ `promeskane` = přeskočil; obchod bez `setup_id` = vlastní.
 */
export function detectorComparison(entries: JournalEntry[]): DetectorComparison {
  let taken = 0
  let skipped = 0
  let own = 0
  for (const entry of entries) {
    if (entry.entry_type === 'promeskane') {
      skipped += 1
    } else if (entry.entry_type === 'obchod') {
      if (entry.setup_id !== null) taken += 1
      else own += 1
    }
  }
  return { taken, skipped, own }
}

/** Hodnota z kontextu jako klíč skupiny; null = neměřeno. */
export function contextKey(entry: JournalEntry, field: string): string | null {
  const value = entry.context?.[field]
  if (value === null || value === undefined || value === '') return null
  return String(value)
}
