/** Testy heatmap módů/škál — ručně spočtené hodnoty, zrcadlí engine compute/heatmap.py. */
import { expect, test } from 'vitest'
import { buildModeGrid, p99Denominator } from './modes'
import type { RawDay } from './modes'
import { maxPainAt, maxPainSeries } from './maxpain'

/** 1 minuta, strikes [90, 100, 110], spot 100 (index = strikeIdx, minutes=1). */
function raw(overrides: Partial<RawDay> = {}): RawDay {
  return {
    minutes: 1,
    strikes: [90, 100, 110],
    callOi: Float32Array.from([100, 200, 300]),
    putOi: Float32Array.from([400, 100, 50]),
    callVolume: Float32Array.from([5, 10, 20]),
    putVolume: Float32Array.from([8, 4, 2]),
    spotSeries: [100],
    staleAge: null,
    ...overrides,
  }
}

test('p99Denominator: p99 absolutních hodnot', () => {
  expect(p99Denominator(Float32Array.from([100, 200, 300]))).toBe(300)
  expect(p99Denominator(Float32Array.from([-400, 100, 50]))).toBe(400)
  expect(p99Denominator(Float32Array.from([]))).toBe(0)
})

test('OI mód: vrstvy normalizované společným p99', () => {
  const grid = buildModeGrid(raw(), 'oi', 'linear')
  // denom = max(p99 call 300, p99 put 400) = 400
  expect(Array.from(grid.layers.call!)).toEqual([0.25, 0.5, 0.75])
  expect(Array.from(grid.layers.put!)).toEqual([1, 0.25, 0.125])
})

function expectClose(values: Float32Array | undefined, expected: number[]): void {
  const actual = Array.from(values!)
  expect(actual).toHaveLength(expected.length)
  actual.forEach((value, index) => expect(value).toBeCloseTo(expected[index], 5))
}

test('Vol OTM: call K > S, put K < S (konvence enginu)', () => {
  const grid = buildModeGrid(raw(), 'vol_otm', 'linear')
  // call OTM jen 110 (20), put OTM jen 90 (8); denom = 20
  expectClose(grid.layers.call, [0, 0, 1])
  expectClose(grid.layers.put, [0.4, 0, 0])
})

test('Vol ITM: doplněk — ATM buňka patří do ITM vrstvy', () => {
  const grid = buildModeGrid(raw(), 'vol_itm', 'linear')
  // call ITM: [5, 10, 0]; put ITM: [0, 4, 2]; denom = 10
  expectClose(grid.layers.call, [0.5, 1, 0])
  expectClose(grid.layers.put, [0, 0.4, 0.2])
})

test('Vol ±: signed vrstva call − put', () => {
  const grid = buildModeGrid(raw(), 'vol_signed', 'linear')
  // [-3, 6, 18], denom 18
  const signed = Array.from(grid.layers.signed!)
  expect(signed[0]).toBeCloseTo(-3 / 18, 5)
  expect(signed[1]).toBeCloseTo(6 / 18, 5)
  expect(signed[2]).toBeCloseTo(1, 5)
})

test('OI−ITM a OI±All', () => {
  const minusItm = buildModeGrid(raw(), 'oi_minus_itm', 'linear')
  // call: [95, 190, 300], put: [400, 96, 48]; denom 400
  expect(Array.from(minusItm.layers.call!)[2]).toBeCloseTo(0.75, 5)
  expect(Array.from(minusItm.layers.put!)[0]).toBeCloseTo(1, 5)

  const signedAll = buildModeGrid(raw(), 'oi_signed_all', 'linear')
  // [-300, 100, 250]; denom 300
  const signed = Array.from(signedAll.layers.signed!)
  expect(signed[0]).toBeCloseTo(-1, 5)
  expect(signed[2]).toBeCloseTo(250 / 300, 5)
})

test('OI+OTM: složky normalizované na společné maximum per minuta', () => {
  const grid = buildModeGrid(raw(), 'oi_plus_otm', 'linear')
  // maxOi=400, maxOtm=20; call: [0.15, 0.3, 0.85], put: [0.76, 0.15, 0.075]; denom 0.85
  const call = Array.from(grid.layers.call!)
  const put = Array.from(grid.layers.put!)
  expect(call[2]).toBeCloseTo(1, 4)
  expect(put[0]).toBeCloseTo(0.76 / 0.85, 4)
})

test('škály zachovávají znaménko (copysign √)', () => {
  const grid = buildModeGrid(raw(), 'vol_signed', 'sqrt')
  // transform: [-√3, √6, √18]; denom √18
  const signed = Array.from(grid.layers.signed!)
  expect(signed[0]).toBeCloseTo(-Math.sqrt(3) / Math.sqrt(18), 4)
  expect(signed[2]).toBeCloseTo(1, 4)
})

test('bez OI (ranní okno CME) OI módy fallbackují na volume', () => {
  const grid = buildModeGrid(
    raw({ callOi: new Float32Array(3), putOi: new Float32Array(3) }),
    'oi',
    'linear',
  )
  // volume: call [5,10,20], put [8,4,2]; denom 20
  expect(Array.from(grid.layers.call!)).toEqual([0.25, 0.5, 1])
})

// ── Max Pain ───────────────────────────────────────────────────────

test('maxPain: symetrické OI → prostřední strike (ručně spočteno)', () => {
  // cost(90)=300, cost(100)=200, cost(110)=300 při OI 10 všude
  const strikes = [90, 100, 110]
  expect(
    maxPainAt(
      strikes,
      () => 10,
      () => 10,
    ),
  ).toBe(100)
})

test('maxPainSeries: bez OI je řada null', () => {
  const series = maxPainSeries(raw({ callOi: new Float32Array(3), putOi: new Float32Array(3) }))
  expect(series).toEqual([null])
  // s OI vrací strike s minimální výplatou
  const withOi = maxPainSeries(raw())
  expect(withOi[0]).not.toBeNull()
  expect([90, 100, 110]).toContain(withOi[0])
})
