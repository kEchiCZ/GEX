/** Testy P/C poměru (#469): jednotky, základy, stale vyloučení, ruční přepočet. */
import { describe, expect, it, test } from 'vitest'
import type { ProfileRow } from './bars'
import { computePcr, formatMoney, topPremiumStrikes } from './pcr'

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

describe('okenní P/C (#486) — parita s API golden testem', () => {
  // Zrcadlí test_profile_window_pc_summary_golden: 3 strikes, okno vol
  // 20·(i+1) per strana, mid 10.25 → premium/strana 120 × 10.25 × M
  const windowRows: ProfileRow[] = [1, 2, 3].map((factor, index) => ({
    strike: 7590 + index * 10,
    callVolComponent: 0,
    callOiComponent: 0,
    putVolComponent: 0,
    putOiComponent: 0,
    callVolume: 20 * factor,
    putVolume: 20 * factor,
    callOi: 100,
    putOi: 150,
    distanceFromSpot: 0,
    callMid: 10.25,
    putMid: 10.25,
    staleAge: 0,
  }))

  it('premium = Σ vol_okna × mid × multiplikátor; kusový poměr vedle', () => {
    const premium = computePcr(windowRows, 'vol', 'premium', 50, 7600)
    expect(premium.call).toBeCloseTo(120 * 10.25 * 50, 6)
    expect(premium.put).toBeCloseTo(120 * 10.25 * 50, 6)
    expect(premium.ratio).toBeCloseTo(1, 6)
    const contracts = computePcr(windowRows, 'vol', 'contracts', 50, 7600)
    expect(contracts.call).toBe(120)
    expect(contracts.ratio).toBeCloseTo(1, 6)
  })

  it('topPremiumStrikes: top 5 dle podílu, stale strany vyloučené', () => {
    const top = topPremiumStrikes(windowRows, 50)
    expect(top).toHaveLength(5) // 6 stran, limit 5
    expect(top[0].premium).toBeCloseTo(60 * 10.25 * 50, 6) // největší strike i=3
    // Podíly se sčítají přes CELEK (i mimo top 5): 60+60+40+40+20 z 240
    expect(top[0].share).toBeCloseTo(60 / 240, 6)
    const stale = topPremiumStrikes(
      windowRows.map((row) => ({ ...row, staleAge: 9999 })),
      50,
    )
    expect(stale).toEqual([])
  })
})

describe('rozsah striků (#645)', () => {
  // spot 7600: call 7590 je ITM, call 7610 OTM; put naopak
  const rows: ProfileRow[] = [
    row({ strike: 7590, callVolume: 10, putVolume: 10, callMid: 20, putMid: 2, staleAge: 0 }),
    row({ strike: 7610, callVolume: 10, putVolume: 10, callMid: 2, putMid: 20, staleAge: 0 }),
  ]

  it('otm: ITM strany se vynechají úplně (kontrakty i prémie)', () => {
    const premium = computePcr(rows, 'vol', 'premium', 1, 7600, undefined, 'otm')
    expect(premium.call).toBeCloseTo(10 * 2, 6) // jen call 7610
    expect(premium.put).toBeCloseTo(10 * 2, 6) // jen put 7590
    const contracts = computePcr(rows, 'vol', 'contracts', 1, 7600, undefined, 'otm')
    expect(contracts.call).toBe(10)
    expect(contracts.put).toBe(10)
  })

  it('timevalue: z ITM prémie zbyde mid − intrinsic (clamp 0)', () => {
    // call 7590: intrinsic 10, mid 20 → čas. hodnota 10; call 7610 OTM beze změny
    const premium = computePcr(rows, 'vol', 'premium', 1, 7600, undefined, 'timevalue')
    expect(premium.call).toBeCloseTo(10 * 10 + 10 * 2, 6)
    expect(premium.put).toBeCloseTo(10 * 2 + 10 * 10, 6) // put 7610: intrinsic 10 z mid 20
  })

  it('bez spotu degradují otm i timevalue na Vše', () => {
    const all = computePcr(rows, 'vol', 'premium', 1, null, undefined, 'all')
    const otm = computePcr(rows, 'vol', 'premium', 1, null, undefined, 'otm')
    expect(otm).toEqual(all)
  })

  it('topPremiumStrikes respektuje otm rozsah', () => {
    const top = topPremiumStrikes(rows, 1, 5, undefined, 'otm', 7600)
    expect(top.map((t) => `${t.strike}${t.side}`).sort()).toEqual(['7590P', '7610C'])
  })
})
