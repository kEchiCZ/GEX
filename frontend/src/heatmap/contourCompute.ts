/** Kompletní výpočet konturových segmentů (#493): blur → prahy → marching squares.

Vytaženo z Heatmap.tsx do čisté funkce, aby týž kód běžel ve web workeru
(hlavní cesta) i synchronně (fallback bez Workeru — jsdom testy, SSR).
*/
import { contourLevels, flipSegments, marchingSquares } from './contours'
import type { ContoursMode, Segment } from './contours'
import { joinSegments, smoothPolylines } from './polylines'
import type { Polyline } from './polylines'
import { gaussianBlur } from './render'

/** Vyhlazení podél OSY ČASU (#1222): σ v minutách větší než napříč striky,
aby izolinie plynule vlnila místo schodů minutové mřížky. 1D Gauss po řádcích. */
export const TIME_BLUR_RADIUS = 4

export function blurAlongTime(
  field: Float32Array,
  width: number,
  height: number,
  radius = TIME_BLUR_RADIUS,
): Float32Array {
  if (radius <= 0 || width < 3) return field
  const sigma = radius / 1.5
  const kernel: number[] = []
  let sum = 0
  for (let offset = -radius; offset <= radius; offset += 1) {
    const weight = Math.exp(-(offset * offset) / (2 * sigma * sigma))
    kernel.push(weight)
    sum += weight
  }
  const out = new Float32Array(field.length)
  for (let y = 0; y < height; y += 1) {
    const row = y * width
    for (let x = 0; x < width; x += 1) {
      let acc = 0
      let norm = 0
      for (let k = 0; k < kernel.length; k += 1) {
        const sx = x + k - radius
        if (sx < 0 || sx >= width) continue
        acc += field[row + sx] * kernel[k]
        norm += kernel[k]
      }
      out[row + x] = norm > 0 ? acc / norm : 0
    }
  }
  void sum
  return out
}

export function computeContourSegments(
  field: Float32Array,
  width: number,
  height: number,
  mode: ContoursMode,
): Segment[] {
  if (mode === 'off') return []
  const smoothed = gaussianBlur(blurAlongTime(field, width, height), width, height)
  // Kontura flipu (#1174) jde vždy samostatným výpočtem (jiný styl čáry) —
  // kombinované módy sem posílá hook rozložené na hladiny + 'flip'
  if (mode === 'flip') return flipSegments(smoothed, width, height)
  // Prahy per strana nad znaménkovým polem (#571); záporná strana jedním
  // algoritmem nad -field (#570) — u čistě kladných polí je sada prázdná
  const levels = contourLevels(smoothed, mode)
  const segments = levels.positive.flatMap((level) =>
    marchingSquares(smoothed, width, height, level),
  )
  if (levels.negative.length > 0) {
    const negated = Float32Array.from(smoothed, (value) => -value)
    for (const level of levels.negative) {
      segments.push(...marchingSquares(negated, width, height, level))
    }
  }
  return segments
}

/** Kontury jako souvislé vyhlazené křivky (#1222): segmenty → napojení → Chaikin. */
export function computeContourPolylines(
  field: Float32Array,
  width: number,
  height: number,
  mode: ContoursMode,
): Polyline[] {
  return smoothPolylines(joinSegments(computeContourSegments(field, width, height, mode)))
}

/** Segmenty ↔ plochý buffer (transferable přes worker boundary). */
export function segmentsToFlat(segments: Segment[]): Float32Array {
  const flat = new Float32Array(segments.length * 4)
  segments.forEach(([x1, y1, x2, y2], index) => {
    flat[index * 4] = x1
    flat[index * 4 + 1] = y1
    flat[index * 4 + 2] = x2
    flat[index * 4 + 3] = y2
  })
  return flat
}

export function flatToSegments(flat: Float32Array): Segment[] {
  const segments: Segment[] = []
  for (let index = 0; index + 3 < flat.length; index += 4) {
    segments.push([flat[index], flat[index + 1], flat[index + 2], flat[index + 3]])
  }
  return segments
}
