/** Diferenční profil B − A (#489) — čisté funkce.

„Co se změnilo mezi pre-eventem a post-eventem": rozdíl OKENNÍCH objemů per
strike a strana, `vol_window_B(K,s) − vol_window_A(K,s)`. Záměrně SUROVÉ
objemy, ne Δ-vážené komponenty — |Δ| se mezi t2(A) a t2(B) liší a vážení by
do rozdílu zaneslo změnu delty, ne aktivity (posudek #489). OI složky diff
nemají z podstaty (OI je v oknech statické, #483).
*/
import type { ProfileRow } from './bars'

export interface DiffRow {
  strike: number
  /** Kladné = v okně B víc call aktivity než v A; záporné = pokles. */
  callDelta: number
  putDelta: number
}

export function diffProfileRows(rowsA: ProfileRow[], rowsB: ProfileRow[]): DiffRow[] {
  const byStrikeA = new Map(rowsA.map((row) => [row.strike, row]))
  const strikes = new Set<number>()
  for (const row of rowsA) strikes.add(row.strike)
  for (const row of rowsB) strikes.add(row.strike)
  const byStrikeB = new Map(rowsB.map((row) => [row.strike, row]))
  return [...strikes]
    .sort((a, b) => a - b)
    .map((strike) => {
      const a = byStrikeA.get(strike)
      const b = byStrikeB.get(strike)
      return {
        strike,
        callDelta: (b?.callVolume ?? 0) - (a?.callVolume ?? 0),
        putDelta: (b?.putVolume ?? 0) - (a?.putVolume ?? 0),
      }
    })
}

/** Největší |hodnota| napříč stranami — měřítko divergentních pruhů. */
export function diffPeak(rows: DiffRow[]): number {
  let peak = 0
  for (const row of rows) {
    peak = Math.max(peak, Math.abs(row.callDelta), Math.abs(row.putDelta))
  }
  return peak
}
