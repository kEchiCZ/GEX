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
