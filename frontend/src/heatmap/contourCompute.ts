/** Kompletní výpočet konturových segmentů (#493): blur → prahy → marching squares.

Vytaženo z Heatmap.tsx do čisté funkce, aby týž kód běžel ve web workeru
(hlavní cesta) i synchronně (fallback bez Workeru — jsdom testy, SSR).
*/
import { contourLevels, marchingSquares } from './contours'
import type { ContoursMode, Segment } from './contours'
import { gaussianBlur } from './render'

export function computeContourSegments(
  field: Float32Array,
  width: number,
  height: number,
  mode: ContoursMode,
): Segment[] {
  if (mode === 'off') return []
  const smoothed = gaussianBlur(field, width, height)
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
