/** Contours (SPEC 7.2, rev. 2026-08-12 — #571 + #570): marching squares nad
vyhlazeným polem, Off / Major / All.

Prahy jsou PODÍL SÍLY, ne kvantily rozlohy: úroveň = podíl × p99 absolutní
hodnoty strany. Kvantil odpovídá na „kolik buněk je slabších", o síle brzdy
neříká nic a práh plave mezi dny. p99 místo maxima, aby jedna odlehlá buňka
nestlačila prahy k nule — a protože p99 je i jmenovatel barev (modes.ts),
kontury sedí na barvu. Jmenovatel se počítá ZVLÁŠŤ per strana (#570): při
dominanci jedné strany by slabší jinak neměla ani jednu čáru. */
import { selectKth } from './select'

/** Módy: hladiny (Major/All, #571), kontura flipu (#1174: nulová izolinie
znaménkového pole — kde model přechází z tlumení do zesilování, v čase i přes
projekci) a kombinace. `flip` sám je i interní mód pro výpočet flip segmentů. */
export type ContoursMode = 'off' | 'major' | 'all' | 'flip' | 'major+flip' | 'all+flip'
export const CONTOURS_MODES: readonly ContoursMode[] = [
  'off',
  'major',
  'all',
  'flip',
  'major+flip',
  'all+flip',
]

/** Rozklad módu na hladiny (Major/All/off) a příznak flipu — kreslí se
každé jiným stylem, proto dva samostatné výpočty (#1174). */
export function splitContoursMode(mode: ContoursMode): {
  levels: 'off' | 'major' | 'all'
  flip: boolean
} {
  switch (mode) {
    case 'major':
    case 'major+flip':
      return { levels: 'major', flip: mode !== 'major' }
    case 'all':
    case 'all+flip':
      return { levels: 'all', flip: mode !== 'all' }
    case 'flip':
      return { levels: 'off', flip: true }
    default:
      return { levels: 'off', flip: false }
  }
}

/** Úsečka v souřadnicích buněk: [x1, y1, x2, y2]. */
export type Segment = [number, number, number, number]

/** Prahy jako podíl z p99 síly strany (#571): spodní čára = tady tlumení
začíná, horní = tady už je silné. Vždy dvě úrovně; ladí se tady. */
export const CONTOUR_MAJOR: readonly number[] = [0.65, 0.95]
export const CONTOUR_ALL: readonly number[] = [0.4, 0.7]

/** Kvantil (nearest-rank) výběrem nad kopií, ne řazením (#1420, vzor #142). */
export function quantile(values: ArrayLike<number>, q: number): number {
  return quantileInPlace(Float64Array.from(values), q)
}

/** Kvantil nad vlastním polem, které se výběrem přeuspořádá. */
function quantileInPlace(values: Float64Array, q: number): number {
  if (values.length === 0) return 0
  return selectKth(values, Math.min(values.length - 1, Math.max(0, Math.ceil(q * values.length) - 1))) // prettier-ignore
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
  const { levels: base } = splitContoursMode(mode)
  if (base === 'off') return { positive: [], negative: [] }
  const shares = base === 'major' ? CONTOUR_MAJOR : CONTOUR_ALL
  // Strany do typed polí a p99 výběrem (#1420): JS pole a řazení s komparátorem
  // stály na 1m dni 22–34 ms z výpočtu kontur. Float64 drží hodnoty přesně.
  let positiveCount = 0
  let negativeCount = 0
  for (let index = 0; index < field.length; index += 1) {
    const value = field[index]
    if (value > 0) positiveCount += 1
    else if (value < 0) negativeCount += 1
  }
  const positives = new Float64Array(positiveCount)
  const negatives = new Float64Array(negativeCount)
  positiveCount = 0
  negativeCount = 0
  for (let index = 0; index < field.length; index += 1) {
    const value = field[index]
    if (value > 0) {
      positives[positiveCount] = value
      positiveCount += 1
    } else if (value < 0) {
      negatives[negativeCount] = -value
      negativeCount += 1
    }
  }
  const positiveP99 = quantileInPlace(positives, 0.99)
  const negativeP99 = quantileInPlace(negatives, 0.99)
  return {
    positive: positiveP99 > 0 ? shares.map((share) => share * positiveP99) : [],
    negative: negativeP99 > 0 ? shares.map((share) => share * negativeP99) : [],
  }
}

function interpolate(level: number, a: number, b: number): number {
  return a === b ? 0.5 : (level - a) / (b - a)
}

/** Kontura flipu (#1174): nulová izolinie znaménkového pole, ale jen tam,
kde znaménko OPRAVDU mění strany — čtverec musí mít aspoň jeden roh kladný
a jeden záporný. Čtverce s nulami jen na jedné straně (okraj dat, striky bez
gammy) se přeskočí, jinak by kolem každé prázdné oblasti vznikla falešná
čára. Interpolace k nule je táž jako u hladin. */
export function flipSegments(field: Float32Array, width: number, height: number): Segment[] {
  const segments: Segment[] = []
  for (let y = 0; y < height - 1; y += 1) {
    for (let x = 0; x < width - 1; x += 1) {
      const index = y * width + x
      const topLeft = field[index]
      const topRight = field[index + 1]
      const bottomRight = field[index + width + 1]
      const bottomLeft = field[index + width]
      const positive = topLeft > 0 || topRight > 0 || bottomRight > 0 || bottomLeft > 0
      const negative = topLeft < 0 || topRight < 0 || bottomRight < 0 || bottomLeft < 0
      if (!positive || !negative) continue
      cellSegments(segments, field, width, x, y, 0)
    }
  }
  return segments
}

/** Marching squares: vrací úsečky izolinie pro danou úroveň. */
export function marchingSquares(
  field: Float32Array,
  width: number,
  height: number,
  level: number,
): Segment[] {
  const segments: Segment[] = []
  for (let y = 0; y < height - 1; y += 1) {
    for (let x = 0; x < width - 1; x += 1) cellSegments(segments, field, width, x, y, level)
  }
  return segments
}

/** Jeden čtverec marching squares — sdílí ho hladinová i flip kontura.

Úsečky připisuje do `out`, bez alokace pro prázdný čtverec a bez closures
(#1420): dřív vznikalo pole a dvě funkce na každou buňku, ~100 tisíc na úroveň.
Body na hranách se počítají jen ty, které případ potřebuje, stejnou
aritmetikou jako dřív, takže úsečky jsou bit po bitu stejné. */
function cellSegments(
  out: Segment[],
  field: Float32Array,
  width: number,
  x: number,
  y: number,
  level: number,
): void {
  const index = y * width + x
  const topLeft = field[index]
  const topRight = field[index + 1]
  const bottomRight = field[index + width + 1]
  const bottomLeft = field[index + width]
  let caseIndex = 0
  if (topLeft >= level) caseIndex |= 8
  if (topRight >= level) caseIndex |= 4
  if (bottomRight >= level) caseIndex |= 2
  if (bottomLeft >= level) caseIndex |= 1
  if (caseIndex === 0 || caseIndex === 15) return

  // Body na hranách buňky (parametricky interpolované): top a bottom leží na
  // vodorovné hraně (y celé), left a right na svislé (x celé)
  switch (caseIndex) {
    case 1:
    case 14: // left → bottom
      out.push([x, y + interpolate(level, topLeft, bottomLeft), x + interpolate(level, bottomLeft, bottomRight), y + 1]) // prettier-ignore
      break
    case 2:
    case 13: // bottom → right
      out.push([x + interpolate(level, bottomLeft, bottomRight), y + 1, x + 1, y + interpolate(level, topRight, bottomRight)]) // prettier-ignore
      break
    case 3:
    case 12: // left → right
      out.push([x, y + interpolate(level, topLeft, bottomLeft), x + 1, y + interpolate(level, topRight, bottomRight)]) // prettier-ignore
      break
    case 4:
    case 11: // top → right
      out.push([x + interpolate(level, topLeft, topRight), y, x + 1, y + interpolate(level, topRight, bottomRight)]) // prettier-ignore
      break
    case 5: // left → top, bottom → right
      out.push([x, y + interpolate(level, topLeft, bottomLeft), x + interpolate(level, topLeft, topRight), y]) // prettier-ignore
      out.push([x + interpolate(level, bottomLeft, bottomRight), y + 1, x + 1, y + interpolate(level, topRight, bottomRight)]) // prettier-ignore
      break
    case 6:
    case 9: // top → bottom
      out.push([x + interpolate(level, topLeft, topRight), y, x + interpolate(level, bottomLeft, bottomRight), y + 1]) // prettier-ignore
      break
    case 7:
    case 8: // left → top
      out.push([x, y + interpolate(level, topLeft, bottomLeft), x + interpolate(level, topLeft, topRight), y]) // prettier-ignore
      break
    case 10: // top → right, left → bottom
      out.push([x + interpolate(level, topLeft, topRight), y, x + 1, y + interpolate(level, topRight, bottomRight)]) // prettier-ignore
      out.push([x, y + interpolate(level, topLeft, bottomLeft), x + interpolate(level, bottomLeft, bottomRight), y + 1]) // prettier-ignore
      break
  }
}
