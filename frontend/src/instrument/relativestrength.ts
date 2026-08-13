/** Relativní síla ES vs. NQ (#680, Traders mode, na zkoušku) — čisté funkce.

Normalizovaný spread od US openu: pct = (last − open_ref) / open_ref × 100
per symbol, spread = pct(A) − pct(B) v procentních bodech. Referenční cena je
open prvního baru od US openu; před openem se poctivě padá na začátek seance
a výsledek se značí `fromOpen: false`. Čistá odvozená řada — widget jde
kdykoli odstranit bez následků (režim na zkoušku jako settle watch #603).
*/
import type { BarRow } from '../api/briefing'

export interface RelativeStrength {
  pctA: number
  pctB: number
  /** pct(A) − pct(B) v procentních bodech; kladné = A silnější. */
  spreadPb: number
  /** Reference je US open; false = před openem (od začátku seance). */
  fromOpen: boolean
}

function pctFrom(bars: BarRow[], usOpenMs: number): { pct: number; fromOpen: boolean } | null {
  if (bars.length === 0) return null
  const refBar = bars.find((bar) => Date.parse(bar.ts_min) >= usOpenMs)
  const reference = refBar ?? bars[0]
  const base = reference.open
  if (base <= 0) return null
  const last = bars[bars.length - 1].close
  return { pct: ((last - base) / base) * 100, fromOpen: refBar !== undefined }
}

export function relativeStrength(
  barsA: BarRow[],
  barsB: BarRow[],
  usOpenMs: number,
): RelativeStrength | null {
  const a = pctFrom(barsA, usOpenMs)
  const b = pctFrom(barsB, usOpenMs)
  if (a === null || b === null) return null
  return {
    pctA: a.pct,
    pctB: b.pct,
    spreadPb: a.pct - b.pct,
    fromOpen: a.fromOpen && b.fromOpen,
  }
}

/** Formát „+0,42 %" s čárkou (cs) a explicitním znaménkem. */
export function formatPct(value: number): string {
  const rounded = Math.round(value * 100) / 100
  return `${rounded >= 0 ? '+' : ''}${rounded.toFixed(2).replace('.', ',')}`
}
