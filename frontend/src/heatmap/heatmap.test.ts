/** Testy heatmap jádra (issue #23): barvy, grid, render snapshoty, contours, výkon. */
import { describe, expect, test } from 'vitest'
import { applyStale, blend, callColor, putColor, signedColor } from './color'
import { contourLevels, marchingSquares, quantile } from './contours'
import { buildGrid, cellIndex } from './grid'
import { demoGrid } from './demo'
import { PROJECTION_ALPHA, gaussianBlur, renderGrid } from './render'
import { projectGrid } from './projection'
import type { HeatmapGrid } from './grid'
import type { PixelBuffer } from './render'

function smallGrid(): HeatmapGrid {
  // 4 strikes × 6 minut, call blob nahoře, put dole, jedna stale buňka
  const minutes = 6
  const strikes = [7590, 7595, 7600, 7605]
  const size = minutes * strikes.length
  const call = new Float32Array(size)
  const put = new Float32Array(size)
  const staleAge = new Float32Array(size)
  for (let m = 0; m < minutes; m += 1) {
    call[3 * minutes + m] = 0.9 // strike 7605
    call[2 * minutes + m] = 0.4
    put[0 * minutes + m] = 0.8 // strike 7590
    put[1 * minutes + m] = 0.3
  }
  staleAge[3 * minutes + 5] = 900 // poslední minuta nejvyššího striku je stale
  return { minutes, strikes, layers: { call, put }, staleAge }
}

function bufferToHexRows(buffer: PixelBuffer): string[] {
  const rows: string[] = []
  for (let y = 0; y < buffer.height; y += 1) {
    let row = ''
    for (let x = 0; x < buffer.width; x += 1) {
      const offset = (y * buffer.width + x) * 4
      for (let channel = 0; channel < 4; channel += 1) {
        row += buffer.data[offset + channel].toString(16).padStart(2, '0')
      }
      row += ' '
    }
    rows.push(row.trimEnd())
  }
  return rows
}

// ── Barvy ──────────────────────────────────────────────────────────

test('barevné rampy: intenzita roste s hodnotou, signed diverguje', () => {
  expect(callColor(0)[3]).toBe(0)
  expect(callColor(1)).toEqual([20, 200, 170, 255])
  expect(putColor(1)).toEqual([230, 45, 60, 255])
  expect(signedColor(1)[1]).toBeGreaterThan(signedColor(1)[0]) // kladné → zelená
  expect(signedColor(-1)[0]).toBeGreaterThan(signedColor(-1)[1]) // záporné → červená
  expect(signedColor(0)[3]).toBe(0)
})

test('blend skládá call a put vrstvu, stale buňka je vizuálně odlišná', () => {
  const composed = blend(callColor(0.5), putColor(0.5))
  expect(composed[3]).toBeGreaterThan(0)
  const normal = callColor(0.8)
  const stale = applyStale(normal)
  expect(stale).not.toEqual(normal)
  expect(stale[3]).toBeLessThan(normal[3]) // nižší alfa = viditelně stará data
})

// ── Grid ───────────────────────────────────────────────────────────

test('buildGrid staví matici z buněk a řadí strikes', () => {
  const grid = buildGrid(3, [
    { minuteIdx: 0, strike: 7600, layer: 'call', value: 0.5 },
    { minuteIdx: 2, strike: 7590, layer: 'put', value: 0.7, staleAge: 400 },
  ])
  expect(grid.strikes).toEqual([7590, 7600])
  expect(grid.layers.call?.[cellIndex(grid, 1, 0)]).toBe(0.5)
  expect(grid.layers.put?.[cellIndex(grid, 0, 2)]).toBeCloseTo(0.7)
  expect(grid.staleAge?.[cellIndex(grid, 0, 2)]).toBe(400)
})

// ── Render: vizuální regresní snapshoty módů (AC) ─────────────────

describe('vizuální regresní snapshoty', () => {
  test('gradient call+put vrstvy', () => {
    expect(bufferToHexRows(renderGrid(smallGrid(), 'gradient'))).toMatchSnapshot()
  })

  test('blobs (gaussovský kernel)', () => {
    expect(bufferToHexRows(renderGrid(smallGrid(), 'blobs'))).toMatchSnapshot()
  })

  test('signed vrstva (Vol±/OI±All)', () => {
    const minutes = 4
    const signed = Float32Array.from([0.8, -0.8, 0.2, -0.2, 1, -1, 0, 0.5])
    const grid: HeatmapGrid = {
      minutes,
      strikes: [7595, 7600],
      layers: { signed },
      staleAge: null,
    }
    expect(bufferToHexRows(renderGrid(grid, 'gradient'))).toMatchSnapshot()
  })
})

test('stale buňka se liší od stejné hodnoty bez stale', () => {
  const grid = smallGrid()
  const buffer = renderGrid(grid, 'gradient')
  // strike 7605 je řádek 0 (nejvyšší nahoře); minuta 4 fresh vs. minuta 5 stale
  const fresh = (0 * buffer.width + 4) * 4
  const stale = (0 * buffer.width + 5) * 4
  expect(buffer.data[stale + 3]).toBeLessThan(buffer.data[fresh + 3])
})

test('skalární smyčka renderGrid dává stejné pixely jako helpery z color.ts (#142)', () => {
  // renderGrid má barvy inlinované ve skalárech kvůli výkonu; tento test hlídá,
  // že se matematika nerozešla s referenčními helpery (call+put i signed, se stale).
  const minutes = 23
  const strikeCount = 17
  const size = minutes * strikeCount
  const pseudo = (seed: number, index: number) => ((index * seed) % 211) / 210
  const build = (signedLayer: boolean): HeatmapGrid => {
    const staleAge = new Float32Array(size)
    for (let index = 0; index < size; index += 1) staleAge[index] = index % 5 === 0 ? 900 : 0
    if (signedLayer) {
      const signed = new Float32Array(size)
      for (let index = 0; index < size; index += 1) signed[index] = pseudo(97, index) * 2 - 1
      return { minutes, strikes: Array.from({ length: strikeCount }, (_, i) => i), layers: { signed }, staleAge } // prettier-ignore
    }
    const call = new Float32Array(size)
    const put = new Float32Array(size)
    for (let index = 0; index < size; index += 1) {
      call[index] = pseudo(53, index)
      put[index] = pseudo(89, index)
    }
    return { minutes, strikes: Array.from({ length: strikeCount }, (_, i) => i), layers: { call, put }, staleAge } // prettier-ignore
  }

  for (const signedLayer of [false, true]) {
    const grid = build(signedLayer)
    const buffer = renderGrid(grid, 'gradient')
    for (let y = 0; y < strikeCount; y += 1) {
      const strikeIdx = strikeCount - 1 - y
      for (let x = 0; x < minutes; x += 1) {
        const index = strikeIdx * minutes + x
        let expected = grid.layers.signed
          ? signedColor(grid.layers.signed[index])
          : blend(blend([0, 0, 0, 0], callColor(grid.layers.call![index])), putColor(grid.layers.put![index])) // prettier-ignore
        if (grid.staleAge![index] > 300) expected = applyStale(expected)
        const offset = (y * minutes + x) * 4
        expect([
          buffer.data[offset],
          buffer.data[offset + 1],
          buffer.data[offset + 2],
          buffer.data[offset + 3],
        ]).toEqual(expected)
      }
    }
  }
})

test('renderGrid nad projekcí: výplň řádků je byte-identická s plným výpočtem (#155)', () => {
  // Projekční sloupce se nepočítají per pixel, ale kopírují (copyWithin) —
  // tento test hlídá shodu s referenčním per-pixel výpočtem přes helpery
  // z color.ts včetně stale, PROJECTION_ALPHA a rozmazání u Blobs.
  const dataMinutes = 21
  const extra = 30
  const strikeCount = 13
  const size = dataMinutes * strikeCount
  const pseudo = (seed: number, index: number) => ((index * seed) % 211) / 210
  const call = new Float32Array(size)
  const put = new Float32Array(size)
  const staleAge = new Float32Array(size)
  for (let index = 0; index < size; index += 1) {
    call[index] = pseudo(53, index)
    put[index] = pseudo(89, index)
    staleAge[index] = index % 7 === 0 ? 900 : 0
  }
  const grid: HeatmapGrid = {
    minutes: dataMinutes,
    strikes: Array.from({ length: strikeCount }, (_, i) => i),
    layers: { call, put },
    staleAge,
  }
  const projected = projectGrid(grid, extra)
  const total = projected.minutes

  for (const style of ['gradient', 'blobs'] as const) {
    const buffer = renderGrid(projected, style)
    // Reference: plné rozmazání BEZ zkratky pro konstantní sloupce
    const layerRef = (values: Float32Array): Float32Array =>
      style === 'blobs' ? gaussianBlur(values, total, strikeCount) : values
    const callRef = layerRef(projected.layers.call!)
    const putRef = layerRef(projected.layers.put!)
    for (let y = 0; y < strikeCount; y += 1) {
      const strikeIdx = strikeCount - 1 - y
      for (let x = 0; x < total; x += 1) {
        const index = strikeIdx * total + x
        let expected = blend(blend([0, 0, 0, 0], callColor(callRef[index])), putColor(putRef[index])) // prettier-ignore
        if (projected.staleAge![index] > 300) expected = applyStale(expected)
        if (x >= dataMinutes) expected = [expected[0], expected[1], expected[2], Math.round(expected[3] * PROJECTION_ALPHA)] // prettier-ignore
        const offset = (y * total + x) * 4
        expect(
          [buffer.data[offset], buffer.data[offset + 1], buffer.data[offset + 2], buffer.data[offset + 3]], // prettier-ignore
          `style=${style} x=${x} y=${y}`,
        ).toEqual(expected)
      }
    }
  }
})

// ── Gaussovské rozmazání ───────────────────────────────────────────

test('gaussianBlur s hintem konstantních sloupců == plná konvoluce (#155)', () => {
  const width = 40
  const height = 9
  const constantFromX = 11
  const field = new Float32Array(width * height)
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      // Data část pseudonáhodná, od constantFromX konstantní (jako projekce)
      const value = x < constantFromX ? ((x * 31 + y * 17) % 97) / 96 : ((y * 13) % 7) / 6
      field[y * width + x] = value
    }
  }
  const full = gaussianBlur(field, width, height)
  const hinted = gaussianBlur(field, width, height, 2, constantFromX)
  expect(Array.from(hinted)).toEqual(Array.from(full))
})

test('gaussianBlur má maximum ve zdroji a rozprostírá energii', () => {
  const field = new Float32Array(25)
  field[12] = 1 // střed 5×5
  const blurred = gaussianBlur(field, 5, 5)
  expect(blurred[12]).toBeGreaterThan(blurred[11])
  expect(blurred[11]).toBeGreaterThan(blurred[10])
  const total = blurred.reduce((acc, value) => acc + value, 0)
  expect(total).toBeCloseTo(1, 1) // separabilní kernel je normalizovaný
})

// ── Contours ───────────────────────────────────────────────────────

test('quantile a úrovně izolinií', () => {
  const field = Float32Array.from({ length: 100 }, (_, index) => (index + 1) / 100)
  expect(quantile(field, 0.9)).toBeCloseTo(0.9)
  expect(contourLevels(field, 'off')).toEqual([])
  expect(contourLevels(field, 'major')).toHaveLength(2)
  expect(contourLevels(field, 'all')).toHaveLength(5)
})

test('marching squares najde hranici kolem vrcholu', () => {
  // 4×4 pole s vrcholem uprostřed → izolinie 0.5 obklopí vrchol
  const field = Float32Array.from([0, 0, 0, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 0, 0, 0])
  const segments = marchingSquares(field, 4, 4, 0.5)
  expect(segments.length).toBeGreaterThanOrEqual(8) // uzavřený obrys
  expect(segments).toMatchSnapshot()
})

test('marching squares: prázdné a plné pole nemá segmenty', () => {
  const flat = new Float32Array(16)
  expect(marchingSquares(flat, 4, 4, 0.5)).toEqual([])
  expect(marchingSquares(flat.fill(1), 4, 4, 0.5)).toEqual([])
})

// ── Výkon (AC: 60 fps na referenčním HW) ──────────────────────────

test('plné překreslení 180×1440 je dostatečně rychlé pro 60 fps pipeline', () => {
  const grid = demoGrid(1440, 180)
  renderGrid(grid, 'gradient') // zahřátí JIT

  const start = performance.now()
  renderGrid(grid, 'gradient')
  const fullRedraw = performance.now() - start

  // Pan/zoom nikdy nepřekresluje data (jen drawImage z cache) → 60 fps drží GPU.
  // Plné překreslení (změna minuty/módu, max 1× za minutu) musí být řádově pod snímkem×10.
  expect(fullRedraw).toBeLessThan(150)
})
