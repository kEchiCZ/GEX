/** Gamma pole pro budoucí okamžiky seance (#834).

Projekce dosud držela poslední naměřený sloupec konstantní (ADR-0006). Jenže
jedna věc se do settle mění DETERMINISTICKY, bez jakéhokoli odhadu: čas do
expirace. Gamma je funkcí τ a s blížící se expirací se koncentruje kolem ATM
— pásmo se zužuje a zvyšuje. Držet ho konstantní není „nic nepředstírat", je
to tvrzení „τ se nemění", které je nepravdivé.

Předpoklad zůstává týž a stejně poctivý jako u zmrazeného sloupce: **OI se do
konce seance nezmění**. Mění se jen τ. Žádná extrapolace trendu, žádný odhad
budoucího toku.

Vzorec je Black–Scholes gamma bez úroku (futures, ADR-0009):

    d1 = (ln(S/K) + σ²τ/2) / (σ√τ)
    Γ  = φ(d1) / (S · σ · √τ)

Signed OI: call +, put − (dealer je na druhé straně) — shodně s enginem
(`compute/gexforward.py`), aby projekce navazovala na naměřenou část.
*/

/** Podlaha τ v sekundách — v okamžiku expirace gamma diverguje. Shodné s
enginem (`gexforward.TAU_FLOOR_S`), ať se obě cesty chovají stejně. */
export const TAU_FLOOR_S = 60
const YEAR_S = 365 * 24 * 3600
const SQRT_2PI = Math.sqrt(2 * Math.PI)

export interface ForwardCell {
  strike: number
  /** Signed OI (call +, put −) — už sečtené přes obě strany striku. */
  signedOi: number
  iv: number
}

/** Gamma jednoho kontraktu pro danou cenu podkladu a zbývající čas. */
export function bsGamma(spot: number, strike: number, iv: number, tauS: number): number {
  if (spot <= 0 || strike <= 0 || iv <= 0) return 0
  const tau = Math.max(tauS, TAU_FLOOR_S) / YEAR_S
  const sqrtTau = Math.sqrt(tau)
  const d1 = (Math.log(spot / strike) + 0.5 * iv * iv * tau) / (iv * sqrtTau)
  return Math.exp(-0.5 * d1 * d1) / (SQRT_2PI * spot * iv * sqrtTau)
}

/** NetGEX(S) na mřížce cen pro JEDEN budoucí okamžik.

`grid` jsou ceny podkladu (osa Y heatmapy), `cells` kontrakty s OI a IV.
Vrací pole stejné délky jako `grid`. */
export function fieldAt(
  grid: number[],
  cells: ForwardCell[],
  secondsToSettle: number,
  multiplier: number,
): Float32Array {
  const out = new Float32Array(grid.length)
  for (let gridIdx = 0; gridIdx < grid.length; gridIdx += 1) {
    const spot = grid[gridIdx]
    let net = 0
    for (const cell of cells) {
      if (cell.signedOi === 0 || cell.iv <= 0) continue
      net += bsGamma(spot, cell.strike, cell.iv, secondsToSettle) * cell.signedOi
    }
    out[gridIdx] = net * multiplier
  }
  return out
}

/** Kolik sekund do settle zbývá v každém projekčním sloupci.

Sloupce navazují na poslední naměřenou minutu po `bucketMinutes`; poslední
nesmí spadnout pod podlahu τ (jinak by gamma divergovala do absurdna). */
export function secondsToSettleSeries(
  lastMinuteMs: number,
  settleMs: number,
  count: number,
  bucketMinutes: number,
): number[] {
  const stepMs = Math.max(1, bucketMinutes) * 60_000
  return Array.from({ length: count }, (_, idx) =>
    Math.max(TAU_FLOOR_S, (settleMs - (lastMinuteMs + (idx + 1) * stepMs)) / 1000),
  )
}
