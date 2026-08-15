/** Futures vrstva deníku (#713) — čisté funkce.

Pořadí zavádění podle poměru přínos/náklad (Jigsaw varuje, že
over-dokumentace zabije deník spolehlivěji než jeho absence):
seance → R v bodech + volatilitní bucket → makro tag → kontrakt a roll.

Basis tu ZÁMĚRNĚ není: GEXLens počítá z opcí na ES futures (FOP), takže
podkladem je přímo obchodovaný kontrakt a všechny úrovně jsou nativně v ES
— není co převádět (viz ADR-0028 dodatek a kap. 18 manuálu).
*/
import { pointValue, priceTick } from '../instrument/tick'
import type { JournalTrade } from '../api/journal'

/** Volatilitní režim seance (ADR-0028) — zrcadlo `VOL_BUCKETS` v enginu. */
export const VOL_BUCKET_LABELS: Record<string, string> = {
  low: 'Klidný trh',
  normal: 'Běžný',
  elevated: 'Zvýšená volatilita',
  crisis: 'Krizový',
}

/** Makro událost — dominantní katalyzátor futures dne (nahrazuje earnings). */
export const MACRO_EVENTS = [
  'clean',
  'fomc',
  'cpi',
  'nfp',
  'ppi',
  'retail_sales',
  'auction',
  'fed_speaker',
  'other',
] as const

export type MacroEvent = (typeof MACRO_EVENTS)[number]

export const MACRO_LABELS: Record<MacroEvent, string> = {
  clean: 'Čistý den',
  fomc: 'FOMC',
  cpi: 'CPI',
  nfp: 'NFP',
  ppi: 'PPI',
  retail_sales: 'Retail sales',
  auction: 'Aukce dluhopisů',
  fed_speaker: 'Fed speaker',
  other: 'Jiná',
}

/** Klíčová slova pro odvození makro tagu z titulku zprávy. */
const MACRO_PATTERNS: Array<[MacroEvent, RegExp]> = [
  ['fomc', /\b(fomc|rate decision|fed funds|powell)\b/i],
  ['cpi', /\b(cpi|consumer price|inflation)\b/i],
  ['nfp', /\b(nonfarm|non-farm|nfp|payroll|unemployment rate)\b/i],
  ['ppi', /\b(ppi|producer price)\b/i],
  ['retail_sales', /\bretail sales\b/i],
  ['auction', /\b(auction|treasury note|treasury bond)\b/i],
  ['fed_speaker', /\b(fed|fomc member|speaks|speech)\b/i],
]

/** Makro tag z titulku; `null` když nic nesedí — nedosazuje se `clean`,
 * protože „nenašel jsem" a „nic se nedělo" jsou různé věci. */
export function macroFromHeadline(headline: string): MacroEvent | null {
  for (const [event, pattern] of MACRO_PATTERNS) {
    if (pattern.test(headline)) return event
  }
  return null
}

/**
 * R vyjádřené v BODECH podkladu — primární jednotka futures deníku.
 *
 * Nezávislé na velikosti kontraktu: 1 R v bodech znamená totéž na ES i MES,
 * zatímco v dolarech se liší 10×. Bez toho růst size maskuje degradaci
 * skillu.
 */
export function riskPoints(trade: JournalTrade): number | null {
  const entry = trade.actual_entry ?? trade.planned_entry
  const stop = trade.planned_stop
  if (entry === null || stop === null) return null
  const risk = Math.abs(entry - stop)
  return risk > 0 ? risk : null
}

/** Zisk/ztráta v bodech (se znaménkem podle směru). */
export function resultPoints(trade: JournalTrade): number | null {
  const { actual_entry: entry, actual_exit: exit } = trade
  if (entry === null || exit === null) return null
  return trade.direction === 'long' ? exit - entry : entry - exit
}

/** Kolik ticků obchod urazil — u scalpů čitelnější než body. */
export function ticksCaptured(trade: JournalTrade, symbol: string): number | null {
  const points = resultPoints(trade)
  if (points === null) return null
  const tick = priceTick(symbol)
  return tick > 0 ? points / tick : null
}

/**
 * P/L na JEDEN kontrakt v dolarech — odděluje skill od size.
 *
 * Počítá se z bodů a multiplikátoru, ne z uloženého P/L: uložený P/L nese
 * skutečnou velikost pozice, takže by dvojnásobná size vypadala jako
 * dvojnásobný skill.
 */
export function pnlPerContract(trade: JournalTrade, symbol: string): number | null {
  const points = resultPoints(trade)
  if (points === null) return null
  return points * pointValue(symbol)
}

/** Komisní drag jako podíl hrubého zisku — u scalpů 10–40 %. */
export function feeShare(trade: JournalTrade): number | null {
  const { gross_pnl: gross, fees } = trade
  if (gross === null || fees === null || gross === 0) return null
  return Math.abs(fees / gross)
}

/**
 * Rozdíl plánované a skutečné velikosti — strukturální under/overrisk.
 *
 * „Chtěl jsem 1,4 ES, vzal jsem 1 ES" není chyba disciplíny, ale důsledek
 * hrubé granularity kontraktu; MES ji dělí na desetiny. Bez tohoto pole se
 * to jeví jako nedodržení plánu.
 */
export function sizeGap(plannedSize: number | null, actualSize: number | null): number | null {
  if (plannedSize === null || actualSize === null) return null
  return actualSize - plannedSize
}

const QUARTER_MONTHS = [2, 5, 8, 11] // Mar, Jun, Sep, Dec (0-based)

function thirdFridayUtc(year: number, month: number): Date {
  const firstDay = new Date(Date.UTC(year, month, 1)).getUTCDay()
  return new Date(Date.UTC(year, month, 1 + ((5 - firstDay + 7) % 7) + 14))
}

/**
 * Je datum v roll týdnu?
 *
 * Likvidita se u ES stěhuje na další kvartál zhruba **týden před expirací**
 * (kolem 2. čtvrtka měsíce expirace). V tom týdnu se mění spready i hloubka
 * knihy, takže slippage z těch dnů kontaminuje statistiku a musí jít
 * odfiltrovat.
 */
export function isRollWeek(date: Date): boolean {
  const year = date.getUTCFullYear()
  for (const month of QUARTER_MONTHS) {
    for (const candidateYear of [year - 1, year, year + 1]) {
      const expiry = thirdFridayUtc(candidateYear, month)
      const rollStart = new Date(expiry.getTime() - 8 * 24 * 60 * 60 * 1000)
      // `thirdFridayUtc` vrací půlnoc — den expirace patří do roll týdne celý
      const rollEnd = new Date(expiry.getTime() + 24 * 60 * 60 * 1000)
      if (date >= rollStart && date < rollEnd) return true
    }
  }
  return false
}
