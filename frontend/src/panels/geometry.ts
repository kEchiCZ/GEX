/** Geometrie spodních panelů (SPEC 7.3) — čisté helpery pro SVG. */

/** Výšky sloupců normalizované maximem řady (0..maxHeight). */
export function barHeights(values: number[], maxHeight: number): number[] {
  const peak = Math.max(1e-9, ...values.map((value) => Math.abs(value)))
  return values.map((value) => (Math.abs(value) / peak) * maxHeight)
}

export interface CumDeltaGeometry {
  /** SVG polygon body kladné plochy (nad nulou). */
  positive: string
  /** SVG polygon body záporné plochy (pod nulou). */
  negative: string
  zeroY: number
}

/** Cum Δ jako plocha nad/pod nulou (SPEC 7.3); symetrická škála kolem středu. */
export function cumDeltaAreas(values: number[], width: number, height: number): CumDeltaGeometry {
  const zeroY = height / 2
  if (values.length === 0) {
    return { positive: '', negative: '', zeroY }
  }
  const peak = Math.max(1e-9, ...values.map((value) => Math.abs(value)))
  const scale = zeroY / peak
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
