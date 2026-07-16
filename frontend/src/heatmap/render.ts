/** Render heatmapy do pixel bufferu (SPEC 7.2: Gradient / Blobs, stale odlišení).

Buffer má rozlišení dat (minuty × strikes); pan/zoom kreslí hotový bitmap
přes drawImage s transformací — plné překreslení dat se děje jen při změně
snapshotu/módu, takže 60 fps pan/zoom není limitované touto funkcí.

PixelBuffer je vlastní typ (ne ImageData), aby šel testovat v Node bez canvasu.
*/

import { applyStale, blend, callColor, putColor, signedColor, STALE_THRESHOLD_S } from './color'
import type { Rgba } from './color'
import type { HeatmapGrid } from './grid'

export type HeatmapStyle = 'gradient' | 'blobs'

export interface PixelBuffer {
  width: number
  height: number
  data: Uint8ClampedArray
}

/** Gaussovské rozmazání pole (separabilní kernel) — základ Blobs stylu. */
export function gaussianBlur(
  field: Float32Array,
  width: number,
  height: number,
  radius = 2,
): Float32Array {
  const sigma = radius / 1.5
  const kernel: number[] = []
  let kernelSum = 0
  for (let offset = -radius; offset <= radius; offset += 1) {
    const weight = Math.exp(-(offset * offset) / (2 * sigma * sigma))
    kernel.push(weight)
    kernelSum += weight
  }
  for (let i = 0; i < kernel.length; i += 1) kernel[i] /= kernelSum

  const horizontal = new Float32Array(field.length)
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      let acc = 0
      for (let k = -radius; k <= radius; k += 1) {
        const sx = Math.min(width - 1, Math.max(0, x + k))
        acc += field[y * width + sx] * kernel[k + radius]
      }
      horizontal[y * width + x] = acc
    }
  }
  const result = new Float32Array(field.length)
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      let acc = 0
      for (let k = -radius; k <= radius; k += 1) {
        const sy = Math.min(height - 1, Math.max(0, y + k))
        acc += horizontal[sy * width + x] * kernel[k + radius]
      }
      result[y * width + x] = acc
    }
  }
  return result
}

/** Vykreslí grid do pixel bufferu; řádek 0 = nejvyšší strike (obrazovková orientace). */
export function renderGrid(grid: HeatmapGrid, style: HeatmapStyle): PixelBuffer {
  const width = grid.minutes
  const height = grid.strikes.length
  const layerOf = (values?: Float32Array): Float32Array | undefined =>
    values && style === 'blobs' ? gaussianBlur(values, width, height) : values

  const call = layerOf(grid.layers.call)
  const put = layerOf(grid.layers.put)
  const signed = layerOf(grid.layers.signed)

  const buffer = new Uint8ClampedArray(width * height * 4)
  for (let y = 0; y < height; y += 1) {
    const strikeIdx = height - 1 - y // obrazovka: nahoře nejvyšší strike
    for (let x = 0; x < width; x += 1) {
      const index = strikeIdx * width + x
      let color: Rgba = [0, 0, 0, 0]
      if (signed) {
        color = signedColor(signed[index])
      } else {
        if (call) color = blend(color, callColor(call[index]))
        if (put) color = blend(color, putColor(put[index]))
      }
      if (grid.staleAge && grid.staleAge[index] > STALE_THRESHOLD_S) {
        color = applyStale(color)
      }
      const offset = (y * width + x) * 4
      buffer[offset] = color[0]
      buffer[offset + 1] = color[1]
      buffer[offset + 2] = color[2]
      buffer[offset + 3] = color[3]
    }
  }
  return { width, height, data: buffer }
}
