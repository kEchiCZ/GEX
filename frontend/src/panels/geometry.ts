/** Geometrie spodních panelů (SPEC 7.3) — čisté helpery pro SVG. */

/** Vrchol (max |hodnota|) řady pro normalizaci; 1e-9 chrání před dělením nulou. */
export function seriesPeak(values: (number | null)[]): number {
  return Math.max(1e-9, ...values.map((value) => Math.abs(value ?? 0)))
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
extrém končí `pad` px od okraje pásu. Řada bez děr = jeden úsek. */
export function cumDeltaAreas(
  values: number[],
  width: number,
  height: number,
  pad = CUM_DELTA_PAD,
): CumDeltaGeometry {
  const { segments, zeroY } = cumDeltaSegments(values, width, height, pad)
  return {
    positive: segments[0]?.positive ?? '',
    negative: segments[0]?.negative ?? '',
    zeroY,
  }
}

/** Úsek plochy Cum Δ mezi dvěma mezerami (body polygonů nad a pod nulou). */
export interface CumDeltaSegment {
  positive: string
  negative: string
}

/** Cum Δ s mezerami (ADR-0057 bod 3): null = minuta bez měření, plocha se
přeruší a souvislé úseky se kreslí zvlášť — spojnice přes díru by tvrdila tok,
který nikdo neměřil. Škála a pozice X jsou společné celé řadě, takže úseky
na sebe sedí jako jedna plocha. */
export function cumDeltaSegments(
  values: (number | null)[],
  width: number,
  height: number,
  pad = CUM_DELTA_PAD,
): { segments: CumDeltaSegment[]; zeroY: number } {
  const zeroY = height / 2
  const measured = values.filter((value): value is number => value !== null)
  if (measured.length === 0) return { segments: [], zeroY }
  const peak = Math.max(1e-9, ...measured.map((value) => Math.abs(value)))
  const scale = Math.max(0, zeroY - pad) / peak
  const step = width / values.length
  const x = (index: number) => (index + 0.5) * step
  const segments: CumDeltaSegment[] = []
  let start = -1
  for (let index = 0; index <= values.length; index += 1) {
    const value = index < values.length ? values[index] : null
    if (value !== null && start < 0) start = index
    if (value !== null || start < 0) continue
    const part = values.slice(start, index) as number[]
    const positivePoints = part.map((v, offset) => `${x(start + offset)},${zeroY - Math.max(0, v) * scale}`) // prettier-ignore
    const negativePoints = part.map((v, offset) => `${x(start + offset)},${zeroY - Math.min(0, v) * scale}`) // prettier-ignore
    const baselineStart = `${x(start)},${zeroY}`
    const baselineEnd = `${x(index - 1)},${zeroY}`
    segments.push({
      positive: `${baselineStart} ${positivePoints.join(' ')} ${baselineEnd}`,
      negative: `${baselineStart} ${negativePoints.join(' ')} ${baselineEnd}`,
    })
    start = -1
  }
  return { segments, zeroY }
}

/** CVD podkladu jako linka přes plochu opčního toku (#829).

Vlastní normalizace, ne sdílená s plochou: opční delta tok je v deltách ×
multiplikátor, CVD v kontraktech futures — společná škála by jednu z řad
zploštila k nule. Čte se proto tvar a poloha vůči nule, ne absolutní výška;
proto taky vlastní hodnota v hlavičce panelu. Prázdná řada → prázdný path. */
export function cvdLinePoints(
  values: (number | null)[],
  width: number,
  height: number,
  pad = CUM_DELTA_PAD,
): string {
  const zeroY = height / 2
  const known = values.filter((value): value is number => value !== null)
  if (known.length === 0) {
    return ''
  }
  const peak = Math.max(1e-9, ...known.map((value) => Math.abs(value)))
  const scale = Math.max(0, zeroY - pad) / peak
  const step = width / values.length
  // Minuty bez CVD (výpadek tasty větve) se přeskočí — linka se přeruší,
  // místo aby propadla k nule a předstírala vyrovnaný tok
  return values
    .map((value, index) =>
      value === null ? null : `${(index + 0.5) * step},${zeroY - value * scale}`,
    )
    .filter((point): point is string => point !== null)
    .join(' ')
}

/** Svíčka sentimentu v Daily pohledu (#296, SPEC 7.1); null = den bez dat. */
export interface SentimentCandle {
  open: number
  high: number
  low: number
  close: number
  /** Práh korekce (#565) v SUROVÝCH jednotkách svíčky = correction_level_z × σ dne;
  null = škála nebo práh chybí. V σ ho nese popisek, ne osa (éry řady, #640). */
  threshold?: number | null
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
): { geoms: CandleGeom[]; zeroY: number; thresholdPoints: string } {
  const zeroY = height / 2
  const present = candles.filter((candle): candle is SentimentCandle => candle !== null)
  if (present.length === 0) return { geoms: [], zeroY, thresholdPoints: '' }
  // Špička osy včetně prahu: linie mimo rozsah svíček by se ořízla, ne nakreslila
  const peak = Math.max(
    1e-9,
    ...present.flatMap((candle) => [
      Math.abs(candle.high),
      Math.abs(candle.low),
      ...(candle.threshold != null ? [Math.abs(candle.threshold)] : []),
    ]),
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
  return { geoms, zeroY, thresholdPoints: sentimentThresholdPoints(candles, step, y) }
}

/** Schodovitá linie prahu korekce (#565) přes sloupce-dny: každý den má vlastní
úroveň (20denní maximum se posouvá), takže „H → V" segmenty jako u Evo OI —
šikmá spojnice by tvrdila průběh, který neexistuje. Dny bez prahu linii přeruší
(prázdný řetězec = žádná linie). */
export function sentimentThresholdPoints(
  candles: (SentimentCandle | null)[],
  step: number,
  toY: (value: number) => number,
): string {
  const parts: string[] = []
  candles.forEach((candle, index) => {
    const value = candle?.threshold
    if (value == null) return
    const yValue = toY(value).toFixed(1)
    parts.push(
      `${(index * step).toFixed(1)},${yValue} ${((index + 1) * step).toFixed(1)},${yValue}`,
    )
  })
  return parts.join(' ')
}

// ── Evo OI (#573) ──────────────────────────────────────────────────

/** Zobrazovaná řada Evo OI: Δ od začátku osy (default — absolutní OI se mění
málo a byl by opticky plochý), nebo absolutní úroveň. */
export function evoOiDisplay(series: (number | null)[], mode: 'delta' | 'abs'): (number | null)[] {
  if (mode === 'abs') return series
  const base = series.find((value): value is number => value !== null && value > 0) ?? 0
  return series.map((value) => (value === null ? null : value > 0 || base === 0 ? value - base : 0))
}

/** SVG path schodovité křivky (H → V úseky) — OI se mezi aktualizacemi tick 101
NEinterpoluje: šikmá čára by si vymýšlela průběh, který jsme nenaměřili. */
export function evoOiStepPath(
  series: (number | null)[],
  width: number,
  toY: (value: number) => number,
): string {
  const step = width / Math.max(1, series.length)
  let path = ''
  let previousY: number | null = null
  series.forEach((value, index) => {
    // null = za pozicí přetáčení (#1591): schod končí, nepadá k nule
    if (value === null) {
      previousY = null
      return
    }
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

/** Důvod, proč panel Cum Δ minutu nekreslí (ADR-0057 bod 8). */
export const CUMDELTA_MIDPOINT_REASON = 'midpoint, obrácené znaménko (#1018)'

/** Řada Cum Δ k vykreslení (ADR-0057 bod 8): s `hideMidpoint` (běžící engine
nese tok z tisků) jsou minuty s midpoint znaménkem null a `hiddenReason` nese
důvod pro legendu; mezery (null) zůstávají mezerami. Sdílí panel i chip okna. */
export function visibleCumDelta(
  values: (number | null)[],
  midpoint: boolean[] | undefined,
  hideMidpoint: boolean,
): {
  values: (number | null)[]
  hiddenReason: string | null
  hiddenAt: (index: number) => boolean
} {
  const hidden = (index: number): boolean =>
    hideMidpoint && midpoint !== undefined && midpoint[index] === true
  if (!hideMidpoint || midpoint === undefined || !midpoint.some(Boolean)) {
    return { values, hiddenReason: null, hiddenAt: () => false }
  }
  return {
    values: values.map((value, index) => (hidden(index) ? null : value)),
    hiddenReason: CUMDELTA_MIDPOINT_REASON,
    hiddenAt: hidden,
  }
}
