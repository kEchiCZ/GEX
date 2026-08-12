/** Testy P/C poměru (#469): jednotky, základy, stale vyloučení, ruční přepočet. */
import { expect, test } from 'vitest'
import type { ProfileRow } from './bars'
import { computePcr, formatMoney } from './pcr'

function row(overrides: Partial<ProfileRow> & { strike: number }): ProfileRow {
  return {
    callVolComponent: 0,
    callOiComponent: 0,
    putVolComponent: 0,
    putOiComponent: 0,
    callVolume: 0,
    putVolume: 0,
    callOi: 0,
    putOi: 0,
    distanceFromSpot: 0,
    ...overrides,
  }
}

const ROWS: ProfileRow[] = [
  row({ strike: 7600, callVolume: 100, callOi: 50, callMid: 10, putVolume: 200, putOi: 100, putMid: 4 }), // prettier-ignore
  row({ strike: 7650, callVolume: 40, callOi: 10, callMid: 2, putVolume: 10, putOi: 40, putMid: 20 }), // prettier-ignore
]

test('kontrakty: AC ruční přepočet — Vol + OI, Vol, OI', () => {
  // Vol+OI: call = 100+50+40+10 = 200, put = 200+100+10+40 = 350
  const volOi = computePcr(ROWS, 'vol_oi', 'contracts', 50, 7600)
  expect(volOi.call).toBe(200)
  expect(volOi.put).toBe(350)
  expect(volOi.ratio).toBeCloseTo(350 / 200)
  expect(computePcr(ROWS, 'vol', 'contracts', 50, 7600).call).toBe(140)
  expect(computePcr(ROWS, 'oi', 'contracts', 50, 7600).put).toBe(140)
})

test('prémie: počet × mid × multiplikátor (AC ruční přepočet)', () => {
  // call = (150·10 + 50·2)·50 = 80 000; put = (300·4 + 50·20)·50 = 110 000
  const result = computePcr(ROWS, 'vol_oi', 'premium', 50, 7600)
  expect(result.call).toBe(80_000)
  expect(result.put).toBe(110_000)
  expect(result.ratio).toBeCloseTo(110_000 / 80_000)
  expect(result.missingShare).toBe(0)
})

test('prémie: zmrzlá kotace a chybějící mid se vyloučí a hlásí missingShare', () => {
  const rows = [
    ...ROWS,
    // Zmrzlý strike (stale > práh) — 400 kontraktů ven z výpočtu
    row({ strike: 7700, callVolume: 300, callMid: 99, putVolume: 100, putMid: 99, staleAge: 900 }),
  ]
  const result = computePcr(rows, 'vol_oi', 'premium', 50, 7600)
  expect(result.call).toBe(80_000) // hodnoty beze změny — stale nepřispěl
  expect(result.put).toBe(110_000)
  expect(result.missingShare).toBeCloseTo(400 / 950)
})

test('notional: počet × spot × multiplikátor; bez spotu nula', () => {
  const result = computePcr(ROWS, 'oi', 'notional', 50, 7600)
  expect(result.call).toBe(60 * 7600 * 50)
  expect(result.put).toBe(140 * 7600 * 50)
  expect(computePcr(ROWS, 'oi', 'notional', 50, null).call).toBe(0)
})

test('ratio je null při prázdné call straně (žádné dělení nulou)', () => {
  const puts = [row({ strike: 7600, putVolume: 10, putMid: 5 })]
  expect(computePcr(puts, 'vol', 'contracts', 50, null).ratio).toBeNull()
})

test('formatMoney kompaktně', () => {
  expect(formatMoney(60_900_000)).toBe('$60.9M')
  expect(formatMoney(1_230_000_000)).toBe('$1.2B')
  expect(formatMoney(950)).toBe('$950')
})
