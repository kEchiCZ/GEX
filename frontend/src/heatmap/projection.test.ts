/** Testy projekce heatmapy do settle (ADR-0006). */
import { expect, test } from 'vitest'
import {
  PROJECTION_MAX_MINUTES,
  projectGrid,
  projectionLabels,
  projectionLength,
} from './projection'
import { renderGrid, PROJECTION_ALPHA, PROJECTION_ALPHA_FAR } from './render'
import { dataMinutesOf } from './grid'
import type { HeatmapGrid } from './grid'

/** 3 minuty × 2 strikes; hodnoty rostou v čase, ať je poslední sloupec poznat. */
function grid(): HeatmapGrid {
  const minutes = 3
  const call = Float32Array.from([0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
  const put = Float32Array.from([0.6, 0.5, 0.4, 0.3, 0.2, 0.1])
  const staleAge = Float32Array.from([0, 0, 0, 0, 0, 900])
  return { minutes, strikes: [100, 105], layers: { call, put }, staleAge }
}

test('projectionLength: počet minut do settle, ořezaný stropem', () => {
  const settle = new Date('2026-07-21T20:00:00Z')
  expect(projectionLength('2026-07-21T19:30:00Z', settle)).toBe(30)
  expect(projectionLength('2026-07-21T20:00:00Z', settle)).toBe(0) // už po settle
  expect(projectionLength('2026-07-21T21:00:00Z', settle)).toBe(0)
  // Timeframe koše: 5m koše na 30 minut = 6 sloupců
  expect(projectionLength('2026-07-21T19:30:00Z', settle, 5)).toBe(6)
  // Strop pro vzdálenou expiraci
  expect(projectionLength('2026-07-01T00:00:00Z', settle)).toBe(PROJECTION_MAX_MINUTES)
  // Strop je v minutách reálného času — na 5m timeframe tedy 288 košů, ne 1440 (#156)
  expect(projectionLength('2026-07-01T00:00:00Z', settle, 5)).toBe(PROJECTION_MAX_MINUTES / 5)
  // Bez dat nebo bez settle se neprojektuje
  expect(projectionLength(undefined, settle)).toBe(0)
  expect(projectionLength('2026-07-21T19:30:00Z', null)).toBe(0)
})

/** Float32 nese zaokrouhlovací chybu — porovnáváme s tolerancí. */
function expectClose(values: Float32Array, expected: number[]): void {
  const actual = Array.from(values)
  expect(actual).toHaveLength(expected.length)
  actual.forEach((value, index) => expect(value).toBeCloseTo(expected[index], 5))
}

test('projectGrid drží poslední naměřený sloupec a nemění naměřenou část', () => {
  const source = grid()
  const projected = projectGrid(source, 2)

  expect(projected.minutes).toBe(5)
  expect(dataMinutesOf(projected)).toBe(3)
  // Strike 0: data [0.1, 0.2, 0.3] → projekce drží 0.3
  expectClose(projected.layers.call!.slice(0, 5), [0.1, 0.2, 0.3, 0.3, 0.3])
  // Strike 1: data [0.4, 0.5, 0.6] → projekce drží 0.6
  expectClose(projected.layers.call!.slice(5, 10), [0.4, 0.5, 0.6, 0.6, 0.6])
  expectClose(projected.layers.put!.slice(0, 5), [0.6, 0.5, 0.4, 0.4, 0.4])
  // Zdroj zůstal netknutý
  expect(source.minutes).toBe(3)
  expectClose(source.layers.call!, [0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
})

test('projectGrid bez rozšíření vrací tentýž objekt (stabilní identita)', () => {
  const source = grid()
  expect(projectGrid(source, 0)).toBe(source)
  expect(projectGrid(source, -5)).toBe(source)
})

test('renderGrid kreslí projekci sníženou sytostí, data beze změny', () => {
  const projected = projectGrid(grid(), 2)
  const buffer = renderGrid(projected, 'gradient')
  const alpha = (source: { width: number; data: Uint8ClampedArray }, x: number, y: number) =>
    source.data[(y * source.width + x) * 4 + 3]

  // Řádek 0 = nejvyšší strike (index 1): data končí sloupcem 2 (stale buňka),
  // projekce 3–4. Stáří se do projekce nepřenáší (#156), takže projekce
  // odpovídá FRESH hodnotě téhož sloupce × PROJECTION_ALPHA.
  const noStale: HeatmapGrid = { ...grid(), staleAge: null }
  const freshBuffer = renderGrid(projectGrid(noStale, 2), 'gradient')
  const freshLast = alpha(freshBuffer, 2, 0)
  const lastData = alpha(buffer, 2, 0)
  expect(lastData).toBeGreaterThan(0)
  expect(lastData).toBeLessThan(freshLast) // naměřená stale buňka zůstává ztlumená
  const firstProjected = alpha(buffer, 3, 0)
  expect(firstProjected).toBe(Math.round(freshLast * PROJECTION_ALPHA))
  // Sytost klesá se vzdáleností (#580): poslední sloupec horizontu = _FAR
  const lastProjected = alpha(buffer, 4, 0)
  expect(lastProjected).toBeLessThan(firstProjected)
  expect(lastProjected).toBe(Math.round(freshLast * PROJECTION_ALPHA_FAR))

  // Bez projekce se alfa naměřené části nemění
  const plain = renderGrid(grid(), 'gradient')
  expect(alpha(plain, 2, 0)).toBe(lastData)
})

test('projectGrid: stáří buněk se do projekce nepřenáší (#156)', () => {
  const projected = projectGrid(grid(), 2)
  // Strike 1 (řádky 5–9): data [0, 0, 900], projekce [0, 0]
  expect(Array.from(projected.staleAge!.slice(5, 10))).toEqual([0, 0, 900, 0, 0])
})

test('projectionLabels navazují na poslední naměřenou minutu', () => {
  const labels = projectionLabels('2026-07-21T19:57:00Z', 3, 1, (iso) => iso.slice(11, 16))
  expect(labels).toEqual(['19:58', '19:59', '20:00'])
  // 15m koše
  expect(projectionLabels('2026-07-21T19:00:00Z', 2, 15, (iso) => iso.slice(11, 16))).toEqual([
    '19:15',
    '19:30',
  ])
  expect(projectionLabels(undefined, 3, 1, (iso) => iso)).toEqual([])
})

test('projectionLabels drží wall-clock hranice košů i z nezarovnané minuty (#584)', () => {
  // Poslední naměřená 19:03 leží uvnitř koše 19:00 → projekce začíná na 19:05
  expect(projectionLabels('2026-07-21T19:03:00Z', 3, 5, (iso) => iso.slice(11, 16))).toEqual([
    '19:05',
    '19:10',
    '19:15',
  ])
  // Počet košů do settle se počítá od hranice posledního koše, ne od 19:03
  expect(projectionLength('2026-07-21T19:03:00Z', new Date('2026-07-21T19:15:00Z'), 5)).toBe(3)
})

test('#580: barevná pipeline projekce se nepřepočítává — RGB konstantní, alfa klesá', () => {
  // 1 minuta dat + dlouhý horizont: copyWithin zkratka plní řádek jednou
  // hodnotou; pokles sytosti smí sahat JEN na alfa kanál. Kdyby se pipeline
  // počítala per sloupec, test by chytil každou odchylku RGB.
  const source: HeatmapGrid = {
    minutes: 1,
    strikes: [100, 105],
    layers: { call: Float32Array.from([0.8, 0.4]), put: Float32Array.from([0.2, 0.6]) },
    staleAge: null,
  }
  const projected = projectGrid(source, 40)
  const buffer = renderGrid(projected, 'gradient')
  const px = (x: number, y: number) => {
    const offset = (y * buffer.width + x) * 4
    return Array.from(buffer.data.slice(offset, offset + 4))
  }
  let previousAlpha = Number.POSITIVE_INFINITY
  for (let x = 1; x < buffer.width; x += 1) {
    const [r, g, b, a] = px(x, 0)
    expect([r, g, b], `x=${x}`).toEqual(px(1, 0).slice(0, 3))
    expect(a, `x=${x}`).toBeLessThanOrEqual(previousAlpha)
    previousAlpha = a
  }
  // Konce horizontu: náběžná hrana = PROJECTION_ALPHA, konec = _FAR
  const base = px(0, 0)[3]
  expect(px(1, 0)[3]).toBe(Math.round(base * PROJECTION_ALPHA))
  expect(px(buffer.width - 1, 0)[3]).toBe(Math.round(base * PROJECTION_ALPHA_FAR))
})
