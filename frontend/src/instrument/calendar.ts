/** Měsíční mřížka kalendáře expirací (#513, SPEC 3.2) — čisté funkce bez Reactu.

Kalendář nahrazuje prostý dropdown `YYYYMMDD`: obchodník vybírá expiraci
podle polohy v týdnu/měsíci (pátek = týdenní, třetí pátek = měsíční/kvartální),
což plochý seznam dat neukáže. Mřížka je pondělkem začínající (evropská
konvence) a počítá se v UTC — expirace jsou kalendářní dny bez času.
*/
import { expiryKind } from './expiry'
import type { ExpiryKind } from './expiry'

export interface CalendarDay {
  /** Klíč expirace `YYYYMMDD`; null = den bez expirace. */
  expiry: string | null
  /** Číslo dne v měsíci (1–31). */
  day: number
  /** Patří den do zobrazeného měsíce? Přesahy z okrajů týdnů jsou tlumené. */
  inMonth: boolean
}

/** Klíč `YYYYMMDD` z UTC data. */
export function expiryKeyOf(date: Date): string {
  const y = date.getUTCFullYear()
  const m = String(date.getUTCMonth() + 1).padStart(2, '0')
  const d = String(date.getUTCDate()).padStart(2, '0')
  return `${y}${m}${d}`
}

/** Mřížka měsíce: týdny od pondělí, okraje doplněné dny sousedních měsíců. */
export function monthGrid(
  year: number,
  month: number,
  expiries: ReadonlySet<string>,
): CalendarDay[][] {
  const first = new Date(Date.UTC(year, month, 1))
  // Pondělí = index 0 (getUTCDay má neděli 0)
  const lead = (first.getUTCDay() + 6) % 7
  const cursor = new Date(first)
  cursor.setUTCDate(1 - lead)
  const weeks: CalendarDay[][] = []
  do {
    const week: CalendarDay[] = []
    for (let i = 0; i < 7; i += 1) {
      const key = expiryKeyOf(cursor)
      week.push({
        expiry: expiries.has(key) ? key : null,
        day: cursor.getUTCDate(),
        inMonth: cursor.getUTCMonth() === month && cursor.getUTCFullYear() === year,
      })
      cursor.setUTCDate(cursor.getUTCDate() + 1)
    }
    weeks.push(week)
  } while (cursor.getUTCMonth() === month && cursor.getUTCFullYear() === year)
  return weeks
}

/** Měsíc expirace `YYYYMMDD` → {year, month} (month 0-based); null = špatný formát. */
export function expiryMonth(expiry: string): { year: number; month: number } | null {
  if (!/^\d{8}$/.test(expiry)) return null
  return { year: Number(expiry.slice(0, 4)), month: Number(expiry.slice(4, 6)) - 1 }
}

const MONTH_NAMES = [
  'leden',
  'únor',
  'březen',
  'duben',
  'květen',
  'červen',
  'červenec',
  'srpen',
  'září',
  'říjen',
  'listopad',
  'prosinec',
]

/** Titulek měsíce, např. „srpen 2026". */
export function monthLabel(year: number, month: number): string {
  return `${MONTH_NAMES[month]} ${year}`
}

/** CSS třída druhu expirace — ASCII varianta českých názvů z `expiryKind`. */
export function kindClass(kind: ExpiryKind | null): string {
  switch (kind) {
    case 'denní':
      return 'cal-kind-daily'
    case 'týdenní':
      return 'cal-kind-weekly'
    case 'EOM':
      return 'cal-kind-eom'
    case 'měsíční':
      return 'cal-kind-monthly'
    case 'kvartální':
      return 'cal-kind-quarterly'
    default:
      return ''
  }
}

/** Tooltip dne s expirací: druh + série + případný zdroj tasty. */
export function dayTitle(expiry: string, classes: string[], extended: boolean): string {
  const parts = [expiryKind(expiry) ?? 'expirace']
  if (classes.length > 0) parts.push(classes.join(', '))
  if (extended) parts.push('zdroj tastytrade')
  return parts.join(' · ')
}
