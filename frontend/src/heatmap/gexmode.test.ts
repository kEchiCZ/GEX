/** Testy Dyn GEX 2D módu (ADR-0009 fáze 2): stavba gridu, projekce pole, render. */
import { expect, test } from 'vitest'

import type { GexFieldRow, GexProfileRow } from '../replay/loader'
import { buildGexGrid, projectGexField } from './gexmode'
import { projectGrid } from './projection'
import { renderGrid } from './render'

function row(tsIso: string, values: number[]): GexProfileRow {
  return { tsIso, gridStart: 100, gridStep: 2.5, values }
}

test('buildGexGrid vzorkuje profil na strikes a forward-filluje díry', () => {
  // Mřížka 100..110 po 2.5 → strike 100 = index 0, strike 105 = index 2
  const profiles: (GexProfileRow | null)[] = [
    row('2026-07-22T14:00:00+00:00', [4, 1, 2, 3, -8]),
    null, // minuta bez profilu přebírá poslední známý
    row('2026-07-22T14:02:00+00:00', [2, 0, -4, 0, 0]),
  ]
  const grid = buildGexGrid(profiles, [100, 105], 3, 'linear')

  expect(grid.strikes).toEqual([100, 105])
  expect(grid.staleAge).toBeNull()
  // p99 |hodnot| = 4 → normalizace: [4,4,2] / 4 a [2,2,-4] / 4
  expect([...grid.layers.signed!]).toEqual([1, 1, 0.5, 0.5, 0.5, -1])
})

test('buildGexGrid interpoluje ceny mimo body mřížky', () => {
  // Strike 101.25 leží uprostřed mezi 100 a 102.5 → (0+8)/2 = 4
  const profiles = [row('2026-07-22T14:00:00+00:00', [0, 8])]
  const grid = buildGexGrid(profiles, [100, 101.25], 1, 'linear')
  expect([...grid.layers.signed!]).toEqual([0, 1]) // p99 = 4 → [0/4, 4/4]
})

test('buildGexGrid bez profilů vrací nulovou vrstvu', () => {
  const grid = buildGexGrid([null, null], [100, 105], 2, 'linear')
  expect([...grid.layers.signed!]).toEqual([0, 0, 0, 0])
})

const FIELD: GexFieldRow = {
  tsIso: '2026-07-22T14:01:00+00:00',
  gridStart: 100,
  gridStep: 2.5,
  colStartIso: '2026-07-22T14:02:00+00:00',
  colStepMin: 2,
  colCount: 2,
  // Sloupce za sebou: col0 = [8, 0, -4], col1 = [0, 0, -12]
  values: [8, 0, -4, 0, 0, -12],
}

function measuredGrid() {
  const profiles: (GexProfileRow | null)[] = [row('2026-07-22T14:00:00+00:00', [4, 0, 2]), null]
  return { profiles, grid: buildGexGrid(profiles, [100, 105], 2, 'linear' as const) }
}

test('projectGexField mapuje projekční koše na sloupce pole podle času', () => {
  const { profiles, grid } = measuredGrid()
  const projected = projectGexField(grid, 3, FIELD, {
    profiles,
    lastMinuteIso: '2026-07-22T14:01:00+00:00',
    bucketMinutes: 1,
    scale: 'linear',
  })

  expect(projected.minutes).toBe(5)
  expect(projected.dataMinutes).toBe(2)
  expect(projected.projectionDynamic).toBe(true)
  // Jmenovatel z naměřené části = 4; 14:02→col0, 14:03/14:04→col1
  // strike 100: naměřené [1, 1], pak col0 8/4→clamp 1, col1 0, 0
  // strike 105: naměřené [0.5, 0.5], pak col0 −4/4 = −1, col1 −12/4→clamp −1
  expect([...projected.layers.signed!]).toEqual([1, 1, 1, 0, 0, 0.5, 0.5, -1, -1, -1])
})

test('projectGexField bez pole spadne na konstantní projekci', () => {
  const { profiles, grid } = measuredGrid()
  const opts = {
    profiles,
    lastMinuteIso: '2026-07-22T14:01:00+00:00',
    bucketMinutes: 1,
    scale: 'linear' as const,
  }
  const fallback = projectGexField(grid, 2, null, opts)
  const constant = projectGrid(grid, 2)
  expect([...fallback.layers.signed!]).toEqual([...constant.layers.signed!])
  expect(fallback.projectionDynamic).toBeUndefined()
  // extra <= 0 vrací původní grid (stabilní identita)
  expect(projectGexField(grid, 0, FIELD, opts)).toBe(grid)
})

test('renderGrid s projectionDynamic nekopíruje projekční sloupce', () => {
  // Bez příznaku by render držel od dataMinutes konstantní pixel (ADR-0006
  // zkratka) — dynamické pole má ale v projekci různé hodnoty i znaménka
  const grid = {
    minutes: 3,
    dataMinutes: 1,
    projectionDynamic: true,
    strikes: [100],
    layers: { signed: new Float32Array([0.5, -0.5, 1]) },
    staleAge: null,
  }
  const buffer = renderGrid(grid, 'gradient')
  const pixel = (x: number) => [...buffer.data.slice(x * 4, x * 4 + 4)]
  expect(pixel(1)).not.toEqual(pixel(2))
  expect(pixel(1)[0]).toBe(115) // −0.5 → červená (230·0.5)
  expect(pixel(2)[0]).toBe(24) // +1 → zelená složka, červený kanál 24
})
