/** Render heatmapy do pixel bufferu (SPEC 7.2: Gradient / Blobs, stale odlišení).

Buffer má rozlišení dat (minuty × strikes); pan/zoom kreslí hotový bitmap
přes drawImage s transformací — plné překreslení dat se děje jen při změně
snapshotu/módu, takže 60 fps pan/zoom není limitované touto funkcí.

PixelBuffer je vlastní typ (ne ImageData), aby šel testovat v Node bez canvasu.
*/

import { STALE_THRESHOLD_S } from './color'
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

/** Vykreslí grid do pixel bufferu; řádek 0 = nejvyšší strike (obrazovková orientace).

Vnitřní smyčka počítá barvu ve skalárech místo `Rgba` n-tic: helpery z `color.ts`
alokovaly 3–5 polí na pixel, což přes 250k buněk dělalo ~1M alokací a polovinu času
překreslení (#142). Matematika je s nimi shodná — hlídá to test proti `callColor`
/ `putColor` / `blend` / `applyStale`. */
export function renderGrid(grid: HeatmapGrid, style: HeatmapStyle): PixelBuffer {
  const width = grid.minutes
  const height = grid.strikes.length
  const layerOf = (values?: Float32Array): Float32Array | undefined =>
    values && style === 'blobs' ? gaussianBlur(values, width, height) : values

  const call = layerOf(grid.layers.call)
  const put = layerOf(grid.layers.put)
  const signed = layerOf(grid.layers.signed)
  const staleAge = grid.staleAge

  const buffer = new Uint8ClampedArray(width * height * 4)
  for (let y = 0; y < height; y += 1) {
    const strikeIdx = height - 1 - y // obrazovka: nahoře nejvyšší strike
    for (let x = 0; x < width; x += 1) {
      const index = strikeIdx * width + x
      let r = 0
      let g = 0
      let b = 0
      let a = 0
      if (signed) {
        // signedColor: záporné červeně, kladné zeleně
        const raw = signed[index]
        const v = raw < -1 ? -1 : raw > 1 ? 1 : raw
        if (v >= 0) {
          r = 24
          g = Math.round(190 * v)
          b = Math.round(160 * v)
          a = Math.round(v * 255)
        } else {
          r = Math.round(230 * -v)
          g = Math.round(45 * -v)
          b = Math.round(60 * -v)
          a = Math.round(-v * 255)
        }
      } else {
        if (call) {
          // callColor přes průhledný podklad = beze změny barvy
          const raw = call[index]
          const v = raw < 0 ? 0 : raw > 1 ? 1 : raw
          r = Math.round(20 * v)
          g = Math.round(200 * v)
          b = Math.round(170 * v)
          a = Math.round(v * 255)
        }
        if (put) {
          // putColor alpha-kompozicí přes dosavadní pixel (viz blend v color.ts)
          const raw = put[index]
          const v = raw < 0 ? 0 : raw > 1 ? 1 : raw
          const overR = Math.round(230 * v)
          const overG = Math.round(45 * v)
          const overB = Math.round(60 * v)
          const alphaOver = Math.round(v * 255) / 255
          const alphaBase = (a / 255) * (1 - alphaOver)
          const alpha = alphaOver + alphaBase
          if (alpha === 0) {
            r = 0
            g = 0
            b = 0
            a = 0
          } else {
            r = Math.round((overR * alphaOver + r * alphaBase) / alpha)
            g = Math.round((overG * alphaOver + g * alphaBase) / alpha)
            b = Math.round((overB * alphaOver + b * alphaBase) / alpha)
            a = Math.round(alpha * 255)
          }
        }
      }
      if (staleAge && staleAge[index] > STALE_THRESHOLD_S) {
        // applyStale: desaturace k šedé + snížená alfa
        const gray = Math.round(0.299 * r + 0.587 * g + 0.114 * b)
        r = Math.round(r * 0.35 + gray * 0.65)
        g = Math.round(g * 0.35 + gray * 0.65)
        b = Math.round(b * 0.35 + gray * 0.65)
        a = Math.round(a * 0.55)
      }
      const offset = (y * width + x) * 4
      buffer[offset] = r
      buffer[offset + 1] = g
      buffer[offset + 2] = b
      buffer[offset + 3] = a
    }
  }
  return { width, height, data: buffer }
}
