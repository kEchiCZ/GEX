/** Max Pain: strike minimalizující souhrnnou výplatu držitelům opcí při expiraci.

cost(S) = Σ_K callOI(K)·max(0, S−K) + Σ_K putOI(K)·max(0, K−S); Max Pain je
argmin přes kandidátní S = strikes. Multiplikátor je konstantní faktor —
argmin nemění, proto se vynechává. Počítá se per minuta ze snapshot OI;
bez OI (ranní okno CME, ADR-0001) je řada null a linie se nekreslí.
*/
import type { RawDay } from './modes'

/** Argmin cost(S) přes kandidátní strikes — prefixovými součty v O(strikes).

Naivní dvojitá smyčka je O(strikes²) na každou minutu; přes celý den to byl
zdaleka největší náklad skládání dne (#142: 1000 minut × 256 strikes = 482 ms).
Rozkladem `max(0, ·)` na jednostranné sumy odpadne vnitřní smyčka:

  Σ_{K≤S} callOI(K)·(S−K) = S·Σ_{K≤S} callOI(K) − Σ_{K≤S} callOI(K)·K
  Σ_{K≥S} putOI(K)·(K−S) = Σ_{K≥S} putOI(K)·K − S·Σ_{K≥S} putOI(K)

Obě strany jsou běžící (pre/su-fixové) součty, takže každý kandidát stojí O(1).
Strikes musí být vzestupně seřazené — tak je `RawDay` staví. */
export function maxPainAt(
  strikes: number[],
  callOi: (strikeIdx: number) => number,
  putOi: (strikeIdx: number) => number,
): number | null {
  const count = strikes.length
  let totalOi = 0
  const calls = new Float64Array(count)
  const puts = new Float64Array(count)
  for (let i = 0; i < count; i += 1) {
    // Neseřazený vstup by prefixové součty tiše rozbil — raději hlasitá chyba
    if (i > 0 && strikes[i] < strikes[i - 1]) {
      throw new Error('maxPainAt: strikes musí být vzestupně seřazené')
    }
    calls[i] = callOi(i)
    puts[i] = putOi(i)
    totalOi += calls[i] + puts[i]
  }
  if (totalOi <= 0) return null

  // Call část: běžící součty přes K ≤ S (vzestupně)
  const callCost = new Float64Array(count)
  let callSum = 0
  let callWeighted = 0
  for (let i = 0; i < count; i += 1) {
    callSum += calls[i]
    callWeighted += calls[i] * strikes[i]
    callCost[i] = strikes[i] * callSum - callWeighted
  }
  // Put část: běžící součty přes K ≥ S (sestupně)
  let putSum = 0
  let putWeighted = 0
  const totalCost = new Float64Array(count)
  for (let i = count - 1; i >= 0; i -= 1) {
    putSum += puts[i]
    putWeighted += puts[i] * strikes[i]
    totalCost[i] = callCost[i] + (putWeighted - strikes[i] * putSum)
  }
  // Argmin vzestupně — stejné pořadí jako naivní verze, aby se remízy lámaly stejně
  let best: number | null = null
  let bestCost = Infinity
  for (let i = 0; i < count; i += 1) {
    if (totalCost[i] < bestCost) {
      bestCost = totalCost[i]
      best = strikes[i]
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
