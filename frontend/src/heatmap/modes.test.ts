/** Testy heatmap módů/škál — ručně spočtené hodnoty, zrcadlí engine compute/heatmap.py. */
import { expect, test } from 'vitest'
import { HEATMAP_MODES, buildModeGrid, p99Denominator } from './modes'
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

test('p99Denominator (quickselect) se shoduje s plným tříděním (#142)', () => {
  // Referenční implementace: celé pořadí, stejný index kvantilu
  const reference = (values: Float32Array): number => {
    const sorted = Array.from(values, Math.abs).sort((a, b) => a - b)
    return sorted[Math.max(0, Math.ceil(0.99 * sorted.length) - 1)]
  }
  const cases: Float32Array[] = [
    Float32Array.from([7]), // jediný prvek
    Float32Array.from([5, 5, 5, 5]), // samé duplicity (past quickselectu)
    Float32Array.from({ length: 300 }, () => 0), // samé nuly
    Float32Array.from({ length: 1000 }, (_, i) => i), // rostoucí
    Float32Array.from({ length: 1000 }, (_, i) => -i), // klesající záporné
    Float32Array.from({ length: 999 }, (_, i) => ((i * 7919) % 1013) - 500), // pseudonáhodné
    Float32Array.from({ length: 500 }, (_, i) => (i % 7 === 0 ? 1e6 : (i % 13) - 6)), // odlehlé
  ]
  for (const values of cases) {
    expect(p99Denominator(values)).toBe(reference(values))
  }
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

test('maxPainAt (prefixové součty) se shoduje s naivní O(strikes²) verzí (#142)', () => {
  // Naivní referenční implementace přesně podle definice cost(S)
  const reference = (
    strikes: number[],
    callOi: (i: number) => number,
    putOi: (i: number) => number,
  ): number | null => {
    let totalOi = 0
    for (let i = 0; i < strikes.length; i += 1) totalOi += callOi(i) + putOi(i)
    if (totalOi <= 0) return null
    let best: number | null = null
    let bestCost = Infinity
    for (const settle of strikes) {
      let cost = 0
      for (let i = 0; i < strikes.length; i += 1) {
        cost += callOi(i) * Math.max(0, settle - strikes[i])
        cost += putOi(i) * Math.max(0, strikes[i] - settle)
      }
      if (cost < bestCost) {
        bestCost = cost
        best = settle
      }
    }
    return best
  }

  const strikes = Array.from({ length: 64 }, (_, i) => 7000 + i * 5)
  const scenarios: Array<[string, (i: number) => number, (i: number) => number]> = [
    ['ploché OI (remízy)', () => 10, () => 10],
    ['bez OI', () => 0, () => 0],
    ['jen cally', (i) => (i % 3) + 1, () => 0],
    ['jen puty', () => 0, (i) => (i % 5) + 1],
    ['pseudonáhodné', (i) => (i * 7919) % 900, (i) => (i * 6271) % 700],
    ['špička uprostřed', (i) => (i === 32 ? 5000 : 3), (i) => (i === 32 ? 4000 : 2)],
    ['rostoucí vs. klesající', (i) => i * 11, (i) => (63 - i) * 13],
  ]
  for (const [label, callOi, putOi] of scenarios) {
    expect(maxPainAt(strikes, callOi, putOi), label).toBe(reference(strikes, callOi, putOi))
  }
})

test('maxPainAt: neseřazené strikes selžou hlasitě, ne tichým špatným výsledkem (#142)', () => {
  expect(() =>
    maxPainAt(
      [100, 90, 110],
      () => 10,
      () => 10,
    ),
  ).toThrow(/vzestupně/)
})

test('maxPainSeries: bez OI je řada null', () => {
  const series = maxPainSeries(raw({ callOi: new Float32Array(3), putOi: new Float32Array(3) }))
  expect(series).toEqual([null])
  // s OI vrací strike s minimální výplatou
  const withOi = maxPainSeries(raw())
  expect(withOi[0]).not.toBeNull()
  expect([90, 100, 110]).toContain(withOi[0])
})

test('Dyn GEX není v Mode selectu — je to samostatná vrstva (#242)', () => {
  expect(HEATMAP_MODES.map((mode) => mode.value)).not.toContain('dyn_gex')
})
