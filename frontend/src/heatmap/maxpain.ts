/** Max Pain: strike minimalizující souhrnnou výplatu držitelům opcí při expiraci.

cost(S) = Σ_K callOI(K)·max(0, S−K) + Σ_K putOI(K)·max(0, K−S); Max Pain je
argmin přes kandidátní S = strikes. Multiplikátor je konstantní faktor —
argmin nemění, proto se vynechává. Počítá se per minuta ze snapshot OI;
bez OI (ranní okno CME, ADR-0001) je řada null a linie se nekreslí.
*/
import type { RawDay } from './modes'

export function maxPainAt(
  strikes: number[],
  callOi: (strikeIdx: number) => number,
  putOi: (strikeIdx: number) => number,
): number | null {
  let totalOi = 0
  for (let i = 0; i < strikes.length; i += 1) totalOi += callOi(i) + putOi(i)
  if (totalOi <= 0) return null

  let best: number | null = null
  let bestCost = Infinity
  for (const settle of strikes) {
    let cost = 0
    for (let i = 0; i < strikes.length; i += 1) {
      cost += callOi(i) * Math.max(0, settle - strikes[i])
      cost += putOi(i) * Math.max(0, strikes[i] - settle)
    }
    if (cost < bestCost) {
      bestCost = cost
      best = settle
    }
  }
  return best
}

/** Max Pain per minuta ze surové snapshot matice. */
export function maxPainSeries(raw: RawDay): (number | null)[] {
  const { minutes, strikes } = raw
  return Array.from({ length: minutes }, (_, minuteIdx) =>
    maxPainAt(
      strikes,
      (strikeIdx) => raw.callOi[strikeIdx * minutes + minuteIdx],
      (strikeIdx) => raw.putOi[strikeIdx * minutes + minuteIdx],
    ),
  )
}
