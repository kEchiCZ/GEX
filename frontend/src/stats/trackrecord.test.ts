/** Testy souhrnu track recordu (#298, SPEC 7.3) — čisté funkce. */
import { describe, expect, it } from 'vitest'
import { cagr, groupCurves, maxDrawdown, signalHitRate } from './trackrecord'
import type { SignalRow, TrackRecordRow } from '../api/news'

function row(overrides: Partial<TrackRecordRow>): TrackRecordRow {
  return {
    date: '2026-01-01',
    strategy: 'buy_hold',
    symbol: 'ES',
    equity: 1,
    drawdown: 0,
    ...overrides,
  }
}

describe('groupCurves', () => {
  it('rozdělí per strategie, filtruje symbol a řadí datem', () => {
    const curves = groupCurves(
      [
        row({ date: '2026-01-02', equity: 1.1 }),
        row({ date: '2026-01-01' }),
        row({ strategy: 'state', date: '2026-01-01' }),
        row({ symbol: 'NQ', date: '2026-01-01' }),
      ],
      'ES',
    )
    expect([...curves.keys()].sort()).toEqual(['buy_hold', 'state'])
    expect(curves.get('buy_hold')!.map((r) => r.date)).toEqual(['2026-01-01', '2026-01-02'])
  })
})

describe('cagr', () => {
  it('anualizuje růst mezi prvním a posledním bodem', () => {
    const curve = [row({ date: '2025-01-01', equity: 1 }), row({ date: '2026-01-01', equity: 1.2 })]
    expect(cagr(curve)).toBeCloseTo(1.2 ** (365 / 365) - 1, 6)
    expect(cagr([row({})])).toBeNull()
  })
})

describe('maxDrawdown', () => {
  it('vrací nejhlubší pokles; null drawdowny ignoruje', () => {
    const curve = [
      row({ drawdown: 0 }),
      row({ drawdown: -0.12 }),
      row({ drawdown: null }),
      row({ drawdown: -0.05 }),
    ]
    expect(maxDrawdown(curve)).toBe(-0.12)
    expect(maxDrawdown([row({ drawdown: null })])).toBe(0)
  })
})

describe('signalHitRate', () => {
  it('počítá jen vyhodnocené outcomes daného režimu na okně +5', () => {
    const signal = (mode: 'NEWS' | 'COMBINED', correct: boolean | null): SignalRow => ({
      id: 1,
      ts: '2026-07-29T14:00:00+00:00',
      symbol: 'ES',
      direction: 'long',
      strength: 0.5,
      mode,
      inputs: {},
      expiry_ts: '2026-07-29T18:00:00+00:00',
      outcomes:
        correct === null
          ? []
          : [{ signal_id: 1, window_min: 5, ret_bp: 5, realized_dir: 1, correct, computed_at: '' }],
    })
    const rate = signalHitRate(
      [signal('NEWS', true), signal('NEWS', false), signal('NEWS', null), signal('COMBINED', true)],
      'NEWS',
    )
    expect(rate).toEqual({ hits: 1, total: 2 })
  })
})
