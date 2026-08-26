/** Testy skládacích helperů briefingu (#674) — čisté funkce nad bary/levels. */
import { describe, expect, it } from 'vitest'
import {
  barsRange,
  briefingToPlanText,
  gammaRegimeLabel,
  latestLevels,
  previousStoredDay,
  usOpenMs,
} from './briefing'
import type { BarRow, LevelsRow } from './briefing'

function bar(ts: string, high: number, low: number, close: number): BarRow {
  return { ts_min: ts, open: close, high, low, close, volume: 100 }
}

const BARS: BarRow[] = [
  bar('2026-08-13T04:00:00Z', 6410, 6390, 6400),
  bar('2026-08-13T10:00:00Z', 6425, 6405, 6420),
  bar('2026-08-13T14:00:00Z', 6460, 6415, 6455),
]

describe('barsRange', () => {
  it('extrémy a poslední close přes celou seanci', () => {
    expect(barsRange(BARS)).toEqual({
      high: 6460,
      low: 6390,
      last: 6455,
      lastTs: '2026-08-13T14:00:00Z',
    })
  })

  it('untilMs ořízne na overnight (před US openem)', () => {
    const open = usOpenMs('2026-08-13') // 13:30 UTC v srpnu (EDT)
    const summary = barsRange(BARS, open)
    expect(summary).toEqual({ high: 6425, low: 6390, last: 6420, lastTs: '2026-08-13T10:00:00Z' })
  })

  it('prázdné bary → null', () => {
    expect(barsRange([])).toBeNull()
  })
})

describe('usOpenMs', () => {
  it('9:30 New York = 13:30 UTC v létě (EDT)', () => {
    expect(new Date(usOpenMs('2026-08-13')).toISOString()).toBe('2026-08-13T13:30:00.000Z')
  })

  it('9:30 New York = 14:30 UTC v zimě (EST)', () => {
    expect(new Date(usOpenMs('2026-01-15')).toISOString()).toBe('2026-01-15T14:30:00.000Z')
  })
})

describe('previousStoredDay', () => {
  it('poslední uložený den před datem', () => {
    expect(previousStoredDay(['2026-08-10', '2026-08-11', '2026-08-13'], '2026-08-13')).toBe(
      '2026-08-11',
    )
  })

  it('bez staršího dne → null', () => {
    expect(previousStoredDay(['2026-08-13'], '2026-08-13')).toBeNull()
  })
})

const LEVELS: LevelsRow = {
  ts_min: '2026-08-13T14:00:00Z',
  flip: 6430,
  call_wall: 6500,
  put_wall: 6400,
  centroid: 6445,
  total_gex: 1200,
}

describe('gammaRegimeLabel', () => {
  it('pozitivní gamma nad flipem', () => {
    expect(gammaRegimeLabel(LEVELS, 6455)).toBe('pozitivní gamma (pohyb se tlumí), cena nad flipem')
  })

  it('negativní gamma pod flipem', () => {
    expect(gammaRegimeLabel({ ...LEVELS, total_gex: -50 }, 6410)).toBe(
      'negativní gamma (pohyb se zesiluje), cena pod flipem',
    )
  })

  it('bez levels → bez dat; bez flipu jen znaménko', () => {
    expect(gammaRegimeLabel(null, 6400)).toBe('bez dat')
    expect(gammaRegimeLabel({ ...LEVELS, flip: null }, 6400)).toBe(
      'pozitivní gamma (pohyb se tlumí)',
    )
  })
})

describe('briefingToPlanText', () => {
  it('kostra plánu nese režim, úrovně, včerejšek i overnight', () => {
    const text = briefingToPlanText({
      symbol: 'ES',
      regime: 'pozitivní gamma (pohyb se tlumí), cena nad flipem',
      levels: LEVELS,
      overnight: { high: 6425, low: 6390, last: 6420, lastTs: '' },
      prevDay: { high: 6440, low: 6380, last: 6435, lastTs: '' },
      cliff: { session_date: '2026-08-13', cliff_share: 0.42, is_opex: false },
    })
    expect(text).toContain('Plán dne ES:')
    expect(text).toContain('flip 6430, call wall 6500, put wall 6400')
    expect(text).toContain('settle 6435, rozsah 6380–6440')
    expect(text).toContain('Overnight: 6390–6425')
    expect(text).toContain('odpadá ~42 % gammy')
    expect(text).toContain('Teze dne:')
  })

  it('volatility box (#873): hodnoty když jsou, checkbox rituálu vždy', () => {
    const withData = briefingToPlanText({
      symbol: 'ES',
      regime: 'pozitivní gamma',
      levels: null,
      overnight: null,
      prevDay: null,
      cliff: null,
      vol: { session_date: '2026-08-25', session_range: 42, percentile: 0.54, bucket: 'normal', sample: 252 }, // prettier-ignore
      em: { em: 38.5, anchor: 7600, preOpen: true },
    })
    expect(withData).toContain('Volatilita: normální (p54, 252 seancí) · EM ±38.5 b (0.51 %, pre-open odhad)') // prettier-ignore
    expect(withData).toContain('- [ ] riziko přizpůsobeno režimu (stop/velikost)')

    const withoutData = briefingToPlanText({
      symbol: 'ES',
      regime: 'bez dat',
      levels: null,
      overnight: null,
      prevDay: null,
      cliff: null,
    })
    // Bez dat se říká proč (zásada ADR-0028) — a checkbox zůstává
    expect(withoutData).toContain('Volatilita: bez dat (málo vzorků nebo chybí straddle)')
    expect(withoutData).toContain('- [ ] riziko přizpůsobeno režimu')
  })

  it('latestLevels vrací poslední řádek', () => {
    expect(latestLevels([{ ...LEVELS, flip: 1 }, LEVELS])).toBe(LEVELS)
    expect(latestLevels([])).toBeNull()
  })
})
