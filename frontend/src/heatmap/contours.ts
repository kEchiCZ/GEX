/** Contours (SPEC 7.2, rev. 2026-08-12 — #571 + #570): marching squares nad
vyhlazeným polem, Off / Major / All.

Prahy jsou PODÍL SÍLY, ne kvantily rozlohy: úroveň = podíl × p99 absolutní
hodnoty strany. Kvantil odpovídá na „kolik buněk je slabších", o síle brzdy
neříká nic a práh plave mezi dny. p99 místo maxima, aby jedna odlehlá buňka
nestlačila prahy k nule — a protože p99 je i jmenovatel barev (modes.ts),
kontury sedí na barvu. Jmenovatel se počítá ZVLÁŠŤ per strana (#570): při
dominanci jedné strany by slabší jinak neměla ani jednu čáru. */

export type ContoursMode = 'off' | 'major' | 'all'

/** Úsečka v souřadnicích buněk: [x1, y1, x2, y2]. */
export type Segment = [number, number, number, number]

/** Prahy jako podíl z p99 síly strany (#571): spodní čára = tady tlumení
začíná, horní = tady už je silné. Vždy dvě úrovně; ladí se tady. */
export const CONTOUR_MAJOR: readonly number[] = [0.65, 0.95]
export const CONTOUR_ALL: readonly number[] = [0.4, 0.7]

export function quantile(values: ArrayLike<number>, q: number): number {
  const sorted = Array.from(values).sort((a, b) => a - b)
  if (sorted.length === 0) return 0
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(q * sorted.length) - 1))
  return sorted[index]
}

/** Úrovně izolinií per strana pole — obě sady jsou kladné hodnoty.

`negative` jsou úrovně v |hodnotách| záporné strany: kreslí se jedním
algoritmem nad `-field` (#570), žádná větev s obrácenou logikou. Čistě
kladná pole (OI, Vol) mají zápornou sadu prázdnou — chování beze změny. */
export interface ContourLevels {
  positive: number[]
  negative: number[]
}

export function contourLevels(field: ArrayLike<number>, mode: ContoursMode): ContourLevels {
  if (mode === 'off') return { positive: [], negative: [] }
  const shares = mode === 'major' ? CONTOUR_MAJOR : CONTOUR_ALL
  const positives: number[] = []
  const negatives: number[] = []
  for (let index = 0; index < field.length; index += 1) {
    const value = field[index]
    if (value > 0) positives.push(value)
    else if (value < 0) negatives.push(-value)
  }
  const positiveP99 = quantile(positives, 0.99)
  const negativeP99 = quantile(negatives, 0.99)
  return {
    positive: positiveP99 > 0 ? shares.map((share) => share * positiveP99) : [],
    negative: negativeP99 > 0 ? shares.map((share) => share * negativeP99) : [],
  }
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
