/** Statistika vln pro záložku Stats (#297, SPEC 7.2) — čisté funkce.

Histogram hloubek a délek RiskOn/RiskOff vln, průměr ± σ per směr a poloha
aktuální vlny vůči průměru. Průměrná hloubka RiskOff vln je zároveň adaptivní
práh potvrzení korekce (5.6) — tenhle pohled ho dělá vizuálně čitelným.
*/
import type { WaveRow } from '../api/news'

/** Hloubka vlny ke čtení (#640): σ jednotky, jen když je mají VŠECHNY vlny sady.

Míchat surové hloubky (backfill éra ±0,4) se σ hodnotami (živá éra do ~4)
by histogramy rozbilo hůř než samotný zlom škály — buď kompletně σ, nebo
kompletně raw. Vrací (hodnota per vlna, jsouTo σ jednotky). */
export function depthReading(waves: WaveRow[]): {
  value: (wave: WaveRow) => number
  inSigma: boolean
} {
  const inSigma = waves.length > 0 && waves.every((wave) => wave.depth_z != null)
  return { value: (wave) => (inSigma ? (wave.depth_z ?? wave.depth) : wave.depth), inSigma }
}

export interface DirectionStats {
  count: number
  meanDepth: number
  sigmaDepth: number
  meanLength: number
  /** Hloubky v jednotkách σ (#640)? Jinak surové (fallback před dopočtem σ). */
  inSigma: boolean
}

/** Průměr ± σ hloubky a průměrná délka per směr; σ = 0 pro n < 2. */
export function waveDirectionStats(waves: WaveRow[], direction: string): DirectionStats {
  const reading = depthReading(waves)
  const rows = waves.filter((wave) => wave.direction === direction)
  const count = rows.length
  if (count === 0)
    return { count: 0, meanDepth: 0, sigmaDepth: 0, meanLength: 0, inSigma: reading.inSigma }
  const depths = rows.map(reading.value)
  const meanDepth = depths.reduce((sum, value) => sum + value, 0) / count
  const variance =
    count > 1 ? depths.reduce((sum, value) => sum + (value - meanDepth) ** 2, 0) / (count - 1) : 0
  const meanLength = rows.reduce((sum, wave) => sum + wave.length_days, 0) / count
  return { count, meanDepth, sigmaDepth: Math.sqrt(variance), meanLength, inSigma: reading.inSigma }
}

export interface HistogramBin {
  /** Dolní mez binu (včetně). */
  from: number
  /** Horní mez binu (u posledního včetně). */
  to: number
  count: number
}

/** Histogram s rovnoměrnými biny přes rozsah hodnot; prázdný vstup = []. */
export function histogram(values: number[], binCount: number): HistogramBin[] {
  if (values.length === 0 || binCount <= 0) return []
  const min = Math.min(...values)
  const max = Math.max(...values)
  // Jednobodový rozsah: jeden bin se všemi hodnotami (dělení nulou níž)
  const span = max - min
  if (span === 0) return [{ from: min, to: max, count: values.length }]
  const width = span / binCount
  const bins: HistogramBin[] = Array.from({ length: binCount }, (_, index) => ({
    from: min + index * width,
    to: min + (index + 1) * width,
    count: 0,
  }))
  for (const value of values) {
    const index = Math.min(binCount - 1, Math.floor((value - min) / width))
    bins[index].count += 1
  }
  return bins
}

/** Probíhající vlna (end_date null) daného symbolu; null = žádná. */
export function currentWave(waves: WaveRow[], symbol: string): WaveRow | null {
  return waves.find((wave) => wave.symbol === symbol && wave.end_date === null) ?? null
}
