/** Forward GEX čtení (#572): rozsahy, OPEX, hranice, projekce, tvrdý útes. */
import { expect, test } from 'vitest'
import {
  extendDailyGrid,
  forwardBoundaries,
  futureBlocks,
  isOpexExpiry,
  projectDailyForward,
} from './dailyforward'
import type { ForwardBlock } from './dailyforward'
import type { HeatmapGrid } from './grid'
import { gaussianBlur } from './render'
import type { GexProfileRow } from '../replay/loader'

const block = (day: string, value: number, dropped: string[] = []): ForwardBlock => ({
  day,
  gridStart: 7400,
  gridStep: 100,
  values: [value, value, value],
  droppedExpiries: dropped,
  droppedShare: dropped.length > 0 ? 0.38 : null,
  ivFallbackShare: 0,
})

const BLOCKS = [
  block('2026-08-12', 10),
  block('2026-08-13', 4, ['20260812']),
  block('2026-08-14', 3),
]

test('futureBlocks: jen dny za posledním naměřeným, rozsah filtruje', () => {
  expect(futureBlocks(BLOCKS, '2026-08-12', 'week').map((b) => b.day)).toEqual([
    '2026-08-13',
    '2026-08-14',
  ])
  expect(futureBlocks(BLOCKS, '2026-08-12', 'plus1').map((b) => b.day)).toEqual(['2026-08-13'])
  expect(futureBlocks(BLOCKS, '2026-08-12', 'settle')).toEqual([])
})

test('isOpexExpiry: třetí pátek v měsíci', () => {
  expect(isOpexExpiry('20260821')).toBe(true) // 3. pátek srpna 2026
  expect(isOpexExpiry('20260814')).toBe(false) // 2. pátek
  expect(isOpexExpiry('20260813')).toBe(false) // čtvrtek
})

test('forwardBoundaries: svislice jen tam, kde něco odpadlo', () => {
  const future = futureBlocks(BLOCKS, '2026-08-12', 'week')
  const boundaries = forwardBoundaries(future, 5)
  expect(boundaries).toHaveLength(1)
  expect(boundaries[0].minuteIdx).toBe(5) // první projekční sloupec
  expect(boundaries[0].expiries).toEqual(['20260812'])
  expect(boundaries[0].share).toBe(0.38)
})

test('extendDailyGrid: měřené módy se neprojektují — sloupce prázdné', () => {
  const grid: HeatmapGrid = {
    minutes: 2,
    strikes: [7400, 7500],
    layers: { call: new Float32Array([1, 2, 3, 4]) },
    staleAge: null,
  }
  const extended = extendDailyGrid(grid, 2)
  expect(extended.minutes).toBe(4)
  expect(extended.dataMinutes).toBe(2)
  // řádek strike 0: [1, 2, 0, 0] — projekce prázdná, ne kopie posledního dne
  expect([...extended.layers.call!.subarray(0, 4)]).toEqual([1, 2, 0, 0])
})

test('projectDailyForward: hodnoty z bloků, hranice v hardEdgesX (AC #572)', () => {
  const profiles: (GexProfileRow | null)[] = [
    { tsIso: '2026-08-11T20:00:00Z', gridStart: 7400, gridStep: 100, values: [100, 100, 100] },
    { tsIso: '2026-08-12T20:00:00Z', gridStart: 7400, gridStep: 100, values: [100, 100, 100] },
  ]
  const measured: HeatmapGrid = {
    minutes: 2,
    strikes: [7400, 7500],
    layers: { signed: new Float32Array([1, 1, 1, 1]) },
    staleAge: null,
  }
  const future = futureBlocks(BLOCKS, '2026-08-12', 'week')
  const projected = projectDailyForward(measured, future, {
    profiles,
    scale: 'linear',
    units: 'per_point',
  })
  expect(projected.minutes).toBe(4)
  expect(projected.dataMinutes).toBe(2)
  expect(projected.hardEdgesX).toEqual([2])
  // Jmenovatel = p99 měřené části (100); blok 13. 8. má 4 → 0.04
  const signed = projected.layers.signed!
  expect(signed[2]).toBeCloseTo(0.04, 5) // strike 7400, první projekční sloupec
  expect(signed[3]).toBeCloseTo(0.03, 5)
})

test('gaussianBlur nepřelévá přes tvrdou hranici — útes zůstává plný skok (AC)', () => {
  // 1 řádek: [0, 0, 10, 10]; hranice před sloupcem 2
  const field = new Float32Array([0, 0, 10, 10])
  const soft = gaussianBlur(field, 4, 1, 2, 4)
  const hard = gaussianBlur(field, 4, 1, 2, 4, [2])
  // Bez hranice blur skok rozmaže (hodnota vlevo od hranice > 0)
  expect(soft[1]).toBeGreaterThan(1)
  // S hranicí zůstává vlevo nula a vpravo plných 10 — skok se nesmí vyhladit
  expect(hard[0]).toBeCloseTo(0, 5)
  expect(hard[1]).toBeCloseTo(0, 5)
  expect(hard[2]).toBeCloseTo(10, 5)
  expect(hard[3]).toBeCloseTo(10, 5)
})
