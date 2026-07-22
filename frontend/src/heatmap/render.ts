/** Render heatmapy do pixel bufferu (SPEC 7.2: Gradient / Blobs, stale odlišení).

Buffer má rozlišení dat (minuty × strikes); pan/zoom kreslí hotový bitmap
přes drawImage s transformací — plné překreslení dat se děje jen při změně
snapshotu/módu, takže 60 fps pan/zoom není limitované touto funkcí.

PixelBuffer je vlastní typ (ne ImageData), aby šel testovat v Node bez canvasu.
*/

import { STALE_THRESHOLD_S } from './color'
import { dataMinutesOf } from './grid'
import type { HeatmapGrid } from './grid'

/** Sytost projektované části (ADR-0006) — musí být na první pohled odlišná od dat. */
export const PROJECTION_ALPHA = 0.45

export type HeatmapStyle = 'gradient' | 'blobs'

export interface PixelBuffer {
  width: number
  height: number
  data: Uint8ClampedArray
}

/** Gaussovské rozmazání pole (separabilní kernel) — základ Blobs stylu.

`constantFromX`: sloupce >= tohoto indexu jsou ve VSTUPU identické (projekce
drží poslední naměřený sloupec, ADR-0006). Výstup je pak konstantní podél x
od `constantFromX + radius` — spočítá se jednou a zbytek řádku se vyplní,
místo plné konvoluce přes stovky projekčních sloupců (#155). */
export function gaussianBlur(
  field: Float32Array,
  width: number,
  height: number,
  radius = 2,
  constantFromX = width,
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

  // Od tohoto sloupce je výstup obou průchodů podél x konstantní
  const cut = Math.min(width, Math.max(0, constantFromX) + radius + 1)

  const horizontal = new Float32Array(field.length)
  for (let y = 0; y < height; y += 1) {
    const row = y * width
    for (let x = 0; x < cut; x += 1) {
      let acc = 0
      for (let k = -radius; k <= radius; k += 1) {
        const sx = Math.min(width - 1, Math.max(0, x + k))
        acc += field[row + sx] * kernel[k + radius]
      }
      horizontal[row + x] = acc
    }
    if (cut < width) horizontal.fill(horizontal[row + cut - 1], row + cut, row + width)
  }
  const result = new Float32Array(field.length)
  for (let y = 0; y < height; y += 1) {
    const row = y * width
    for (let x = 0; x < cut; x += 1) {
      let acc = 0
      for (let k = -radius; k <= radius; k += 1) {
        const sy = Math.min(height - 1, Math.max(0, y + k))
        acc += horizontal[sy * width + x] * kernel[k + radius]
      }
      result[row + x] = acc
    }
    if (cut < width) result.fill(result[row + cut - 1], row + cut, row + width)
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
  const dataMinutes = dataMinutesOf(grid)
  // Dyn GEX pole (ADR-0009 fáze 2) má v projekci proměnlivé sloupce — zkratky
  // „konstantní projekce" (blur cut, kopírování řádku) se musí vypnout
  const constantProjection = !grid.projectionDynamic
  const blurRadius = 2
  const layerOf = (values?: Float32Array): Float32Array | undefined =>
    values && style === 'blobs'
      ? gaussianBlur(values, width, height, blurRadius, constantProjection ? dataMinutes : width)
      : values

  const call = layerOf(grid.layers.call)
  const put = layerOf(grid.layers.put)
  const signed = layerOf(grid.layers.signed)
  const staleAge = grid.staleAge

  // Projekce drží poslední naměřený sloupec konstantní (ADR-0006), takže od
  // `fillFrom` je celý řádek bufferu identický — pixel se spočítá jednou a
  // zbytek řádku se zkopíruje. Bez toho projekce násobila náklad překreslení
  // 5–10× (ráno: 120 naměřených vs. ~1300 projekčních sloupců, #155).
  // U Blobs sahá rozmazání přes hranici dat ještě `blurRadius` sloupců.
  const fillFrom =
    dataMinutes < width && constantProjection
      ? Math.min(width - 1, dataMinutes + (style === 'blobs' ? blurRadius : 0))
      : width

  const buffer = new Uint8ClampedArray(width * height * 4)
  for (let y = 0; y < height; y += 1) {
    const strikeIdx = height - 1 - y // obrazovka: nahoře nejvyšší strike
    const computeTo = Math.min(width, fillFrom + 1)
    for (let x = 0; x < computeTo; x += 1) {
      const index = strikeIdx * width + x
      const projected = x >= dataMinutes
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
      // Projekce (ADR-0006): stejná barva, nižší sytost — ať je hned poznat,
      // že vpravo nejsou naměřená data, ale předpoklad „OI se nezmění"
      if (projected) a = Math.round(a * PROJECTION_ALPHA)
      const offset = (y * width + x) * 4
      buffer[offset] = r
      buffer[offset + 1] = g
      buffer[offset + 2] = b
      buffer[offset + 3] = a
    }
    // Zbytek řádku = kopie pixelu na `fillFrom` (exponenciální zdvojování)
    if (computeTo < width) {
      const rowOffset = (y * width + fillFrom) * 4
      const rowLength = (width - fillFrom) * 4
      let filled = 4
      while (filled < rowLength) {
        const chunk = Math.min(filled, rowLength - filled)
        buffer.copyWithin(rowOffset + filled, rowOffset, rowOffset + chunk)
        filled += chunk
      }
    }
  }
  return { width, height, data: buffer }
}
