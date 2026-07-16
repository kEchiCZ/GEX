/** Contours (SPEC 7.2): marching squares nad vyhlazeným polem, Off / Major / All. */

export type ContoursMode = 'off' | 'major' | 'all'

/** Úsečka v souřadnicích buněk: [x1, y1, x2, y2]. */
export type Segment = [number, number, number, number]

export function quantile(values: ArrayLike<number>, q: number): number {
  const sorted = Array.from(values).sort((a, b) => a - b)
  if (sorted.length === 0) return 0
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(q * sorted.length) - 1))
  return sorted[index]
}

/** Úrovně izolinií: Major = p75 a p90; All = 5 úrovní p50–p95 (SPEC 7.2). */
export function contourLevels(field: ArrayLike<number>, mode: ContoursMode): number[] {
  if (mode === 'off') return []
  const positive = Array.from(field).filter((value) => value > 0)
  if (positive.length === 0) return []
  if (mode === 'major') return [quantile(positive, 0.75), quantile(positive, 0.9)]
  return [0.5, 0.65, 0.75, 0.85, 0.95].map((q) => quantile(positive, q))
}

function interpolate(level: number, a: number, b: number): number {
  return a === b ? 0.5 : (level - a) / (b - a)
}

/** Marching squares: vrací úsečky izolinie pro danou úroveň. */
export function marchingSquares(
  field: Float32Array,
  width: number,
  height: number,
  level: number,
): Segment[] {
  const segments: Segment[] = []
  const at = (x: number, y: number) => field[y * width + x]

  for (let y = 0; y < height - 1; y += 1) {
    for (let x = 0; x < width - 1; x += 1) {
      const topLeft = at(x, y)
      const topRight = at(x + 1, y)
      const bottomRight = at(x + 1, y + 1)
      const bottomLeft = at(x, y + 1)
      let caseIndex = 0
      if (topLeft >= level) caseIndex |= 8
      if (topRight >= level) caseIndex |= 4
      if (bottomRight >= level) caseIndex |= 2
      if (bottomLeft >= level) caseIndex |= 1
      if (caseIndex === 0 || caseIndex === 15) continue

      // Body na hranách buňky (parametricky interpolované)
      const top: [number, number] = [x + interpolate(level, topLeft, topRight), y]
      const right: [number, number] = [x + 1, y + interpolate(level, topRight, bottomRight)]
      const bottom: [number, number] = [x + interpolate(level, bottomLeft, bottomRight), y + 1]
      const left: [number, number] = [x, y + interpolate(level, topLeft, bottomLeft)]

      const add = (a: [number, number], b: [number, number]) =>
        segments.push([a[0], a[1], b[0], b[1]])

      switch (caseIndex) {
        case 1:
        case 14:
          add(left, bottom)
          break
        case 2:
        case 13:
          add(bottom, right)
          break
        case 3:
        case 12:
          add(left, right)
          break
        case 4:
        case 11:
          add(top, right)
          break
        case 5:
          add(left, top)
          add(bottom, right)
          break
        case 6:
        case 9:
          add(top, bottom)
          break
        case 7:
        case 8:
          add(left, top)
          break
        case 10:
          add(top, right)
          add(left, bottom)
          break
      }
    }
  }
  return segments
}
