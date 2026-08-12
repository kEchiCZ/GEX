/** Geometrie spodních panelů (SPEC 7.3) — čisté helpery pro SVG. */

/** Vrchol (max |hodnota|) řady pro normalizaci; 1e-9 chrání před dělením nulou. */
export function seriesPeak(values: number[]): number {
  return Math.max(1e-9, ...values.map((value) => Math.abs(value)))
}

/** Výšky sloupců normalizované vrcholem (0..maxHeight).
 *
 * `peak` lze předat explicitně (sdílená škála pro C/P), jinak se bere vrchol řady.
 */
export function barHeights(values: number[], maxHeight: number, peak?: number): number[] {
  const normalizer = peak ?? seriesPeak(values)
  return values.map((value) => (Math.abs(value) / normalizer) * maxHeight)
}

export interface CumDeltaGeometry {
  /** SVG polygon body kladné plochy (nad nulou). */
  positive: string
  /** SVG polygon body záporné plochy (pod nulou). */
  negative: string
  zeroY: number
}

/** Rezerva plochy Cum Δ od okrajů pásu — trendující kumulativní řada jinak
dlouhé úseky „jede po hraně" a vypadá uříznutě (#169). */
export const CUM_DELTA_PAD = 6

/** Cum Δ jako plocha nad/pod nulou (SPEC 7.3); symetrická škála kolem středu,
extrém končí `pad` px od okraje pásu. */
export function cumDeltaAreas(
  values: number[],
  width: number,
  height: number,
  pad = CUM_DELTA_PAD,
): CumDeltaGeometry {
  const zeroY = height / 2
  if (values.length === 0) {
    return { positive: '', negative: '', zeroY }
  }
  const peak = Math.max(1e-9, ...values.map((value) => Math.abs(value)))
  const scale = Math.max(0, zeroY - pad) / peak
  const step = width / values.length
  const x = (index: number) => (index + 0.5) * step

  const positivePoints = values.map(
    (value, index) => `${x(index)},${zeroY - Math.max(0, value) * scale}`,
  )
  const negativePoints = values.map(
    (value, index) => `${x(index)},${zeroY - Math.min(0, value) * scale}`,
  )
  const baselineEnd = `${x(values.length - 1)},${zeroY}`
  const baselineStart = `${x(0)},${zeroY}`
  return {
    positive: `${baselineStart} ${positivePoints.join(' ')} ${baselineEnd}`,
    negative: `${baselineStart} ${negativePoints.join(' ')} ${baselineEnd}`,
    zeroY,
  }
}

/** Svíčka sentimentu v Daily pohledu (#296, SPEC 7.1); null = den bez dat. */
export interface SentimentCandle {
  open: number
  high: number
  low: number
  close: number
}

export interface CandleGeom {
  index: number
  /** Střed svíčky na ose X (základní měřítko, jako sloupce panelů). */
  x: number
  bodyY: number
  bodyHeight: number
  wickY1: number
  wickY2: number
  up: boolean
}

/** Geometrie svíček kolem nulové osy — stejná symetrická škála a rezerva od
okrajů jako plocha Cum Δ/Sentiment, aby obě zobrazení sdílela měřítko čtení. */
export function sentimentCandleGeometry(
  candles: (SentimentCandle | null)[],
  step: number,
  height: number,
  pad = CUM_DELTA_PAD,
): { geoms: CandleGeom[]; zeroY: number } {
  const zeroY = height / 2
  const present = candles.filter((candle): candle is SentimentCandle => candle !== null)
  if (present.length === 0) return { geoms: [], zeroY }
  const peak = Math.max(
    1e-9,
    ...present.flatMap((candle) => [Math.abs(candle.high), Math.abs(candle.low)]),
  )
  const scale = Math.max(0, zeroY - pad) / peak
  const y = (value: number) => zeroY - value * scale
  const geoms: CandleGeom[] = []
  candles.forEach((candle, index) => {
    if (candle === null) return
    const top = y(Math.max(candle.open, candle.close))
    const bottom = y(Math.min(candle.open, candle.close))
    geoms.push({
      index,
      x: (index + 0.5) * step,
      bodyY: top,
      bodyHeight: Math.max(1, bottom - top),
      wickY1: y(candle.high),
      wickY2: y(candle.low),
      up: candle.close >= candle.open,
    })
  })
  return { geoms, zeroY }
}

// ── Evo OI (#573) ──────────────────────────────────────────────────

/** Zobrazovaná řada Evo OI: Δ od začátku osy (default — absolutní OI se mění
málo a byl by opticky plochý), nebo absolutní úroveň. */
export function evoOiDisplay(series: number[], mode: 'delta' | 'abs'): number[] {
  if (mode === 'abs') return series
  const base = series.find((value) => value > 0) ?? 0
  return series.map((value) => (value > 0 || base === 0 ? value - base : 0))
}

/** SVG path schodovité křivky (H → V úseky) — OI se mezi aktualizacemi tick 101
NEinterpoluje: šikmá čára by si vymýšlela průběh, který jsme nenaměřili. */
export function evoOiStepPath(
  series: number[],
  width: number,
  toY: (value: number) => number,
): string {
  const step = width / Math.max(1, series.length)
  let path = ''
  let previousY: number | null = null
  series.forEach((value, index) => {
    const y = toY(value)
    if (previousY === null) {
      path += `M${(index * step).toFixed(1)},${y.toFixed(1)}`
    } else if (y !== previousY) {
      path += `V${y.toFixed(1)}`
    }
    path += `H${((index + 1) * step).toFixed(1)}`
    previousY = y
  })
  return path
}
