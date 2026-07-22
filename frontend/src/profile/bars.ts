/** Geometrie skládaných pruhů strike profilu (SPEC 4.6/7.3) — čisté helpery. */

export interface ProfileRow {
  strike: number
  callVolComponent: number
  callOiComponent: number
  putVolComponent: number
  putOiComponent: number
  /** Surové hodnoty pro tooltip. */
  callVolume: number
  putVolume: number
  callOi: number
  putOi: number
  distanceFromSpot: number
  /** Změna OI proti předchozímu archivovanému dni (null = srovnání není k dispozici). */
  callOiChange?: number | null
  putOiChange?: number | null
}

export interface BarGeometry {
  strike: number
  /** Šířky segmentů v px: call jde doprava od symetrické osy, put doleva. */
  callVolWidth: number
  callOiWidth: number
  putVolWidth: number
  putOiWidth: number
}

/** Největší celková strana (Vol+OI komponenty) — základ měřítka pruhů i osy. */
export function maxComponentSide(rows: ProfileRow[]): number {
  return Math.max(
    1e-9,
    ...rows.map((row) =>
      Math.max(
        row.callVolComponent + row.callOiComponent,
        row.putVolComponent + row.putOiComponent,
      ),
    ),
  )
}

/** Kompaktní formát množství pro osu profilu (1 234 → „1.2k"). */
export function formatAmount(value: number): string {
  if (value >= 1000) {
    const k = value / 1000
    return `${k >= 10 ? Math.round(k) : Math.round(k * 10) / 10}k`
  }
  return String(Math.round(value))
}

/** Zaokrouhlení nahoru na „hezký" násobek (1/2/5 × 10^n) — absolutní škála os. */
export function niceCeil(value: number): number {
  if (value <= 0) return 1
  const exponent = Math.floor(Math.log10(value))
  const base = 10 ** exponent
  const fraction = value / base
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10
  return nice * base
}

/** Dyn GEX křivka v profilu (ADR-0009): SVG path data kladné a záporné části.

Kladná část (dealeři tlumí) jde od středové osy DOPRAVA, záporná (zesilují)
DOLEVA — stejná sémantika stran jako call/put pruhy. Škála na max |hodnota|
profilu. `flipYs` = Y souřadnice průchodů nulou (dynamický flip). */
export interface GexCurve {
  positive: string
  negative: string
  flipYs: number[]
}

export function gexCurvePaths(
  row: { gridStart: number; gridStep: number; values: number[] },
  priceToY: (price: number) => number,
  centerX: number,
  halfSpan: number,
): GexCurve {
  const maxAbs = Math.max(1e-9, ...row.values.map((value) => Math.abs(value)))
  let positive = ''
  let negative = ''
  const flipYs: number[] = []
  let previousSign = 0
  row.values.forEach((value, index) => {
    const price = row.gridStart + index * row.gridStep
    const x = centerX + (value / maxAbs) * halfSpan
    const y = priceToY(price)
    const sign = value >= 0 ? 1 : -1
    const point = `${x.toFixed(1)},${y.toFixed(1)}`
    if (sign >= 0) {
      positive += `${previousSign < 0 || positive === '' ? 'M' : 'L'}${point}`
    } else {
      negative += `${previousSign >= 0 || negative === '' ? 'M' : 'L'}${point}`
    }
    // Průchod nulou mezi sousedními body → lineární interpolace ceny
    if (index > 0 && previousSign !== 0 && sign !== previousSign) {
      const prev = row.values[index - 1]
      const prevPrice = row.gridStart + (index - 1) * row.gridStep
      const zeroPrice = prevPrice + ((0 - prev) / (value - prev)) * row.gridStep
      flipYs.push(priceToY(zeroPrice))
    }
    previousSign = sign
  })
  return { positive, negative, flipYs }
}

/** Šířky pruhů: normalizace referenční stranou (`scaleMax`, default max ve výřezu),
 * zoom násobí, ořez na halfWidth. */
export function barGeometry(
  rows: ProfileRow[],
  halfWidth: number,
  zoom: number,
  scaleMax?: number,
): BarGeometry[] {
  const maxSide = scaleMax ?? maxComponentSide(rows)
  const scale = (halfWidth / maxSide) * zoom
  const clamp = (value: number) => Math.min(halfWidth, value * scale)
  return rows.map((row) => {
    const callVolWidth = clamp(row.callVolComponent)
    const putVolWidth = clamp(row.putVolComponent)
    return {
      strike: row.strike,
      callVolWidth,
      callOiWidth: Math.min(halfWidth - callVolWidth, row.callOiComponent * scale),
      putVolWidth,
      putOiWidth: Math.min(halfWidth - putVolWidth, row.putOiComponent * scale),
    }
  })
}
