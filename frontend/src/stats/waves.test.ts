/** Testy statistiky vln (#297, SPEC 7.2) — čisté funkce. */
import { describe, expect, it } from 'vitest'
import { currentWave, depthReading, histogram, waveDirectionStats } from './waves'
import type { WaveRow } from '../api/news'

function wave(overrides: Partial<WaveRow>): WaveRow {
  return {
    id: 1,
    symbol: 'ES',
    direction: 'RiskOff',
    start_date: '2026-07-01',
    end_date: '2026-07-05',
    depth: 1,
    length_days: 4,
    ...overrides,
  }
}

describe('waveDirectionStats', () => {
  it('počítá průměr ± σ hloubky a průměrnou délku per směr', () => {
    const waves = [
      wave({ id: 1, depth: 1, length_days: 2 }),
      wave({ id: 2, depth: 3, length_days: 6 }),
      wave({ id: 3, direction: 'RiskOn', depth: 100 }), // jiný směr se nepočítá
    ]
    const stats = waveDirectionStats(waves, 'RiskOff')
    expect(stats.count).toBe(2)
    expect(stats.meanDepth).toBe(2)
    expect(stats.sigmaDepth).toBeCloseTo(Math.SQRT2, 6) // výběrová σ z [1, 3]
    expect(stats.meanLength).toBe(4)
  })

  it('prázdný směr → nuly, jedna vlna → σ = 0', () => {
    expect(waveDirectionStats([], 'RiskOn').count).toBe(0)
    expect(waveDirectionStats([wave({})], 'RiskOff').sigmaDepth).toBe(0)
  })
})

describe('histogram', () => {
  it('rozdělí hodnoty do rovnoměrných binů, maximum padne do posledního', () => {
    const bins = histogram([0, 1, 2, 3, 4], 2)
    expect(bins).toHaveLength(2)
    expect(bins[0].count).toBe(2) // 0, 1
    expect(bins[1].count).toBe(3) // 2, 3, 4 (max včetně)
    expect(bins[0].from).toBe(0)
    expect(bins[1].to).toBe(4)
  })

  it('jednobodový rozsah = jeden bin; prázdný vstup = []', () => {
    expect(histogram([2, 2, 2], 5)).toEqual([{ from: 2, to: 2, count: 3 }])
    expect(histogram([], 5)).toEqual([])
  })
})

describe('currentWave', () => {
  it('vrací probíhající vlnu symbolu (end_date null)', () => {
    const waves = [
      wave({ id: 1 }),
      wave({ id: 2, end_date: null, symbol: 'NQ' }),
      wave({ id: 3, end_date: null }),
    ]
    expect(currentWave(waves, 'ES')?.id).toBe(3)
    expect(currentWave([wave({})], 'ES')).toBeNull()
  })
})

describe('depthReading (#640)', () => {
  const wave = (depth: number, depth_z: number | null): WaveRow => ({
    id: 1,
    symbol: 'ES',
    direction: 'RiskOn',
    start_date: '2026-08-01',
    end_date: '2026-08-05',
    depth,
    depth_z,
    length_days: 4,
  })

  it('σ jednotky jen když je mají všechny vlny — jednotky se nesmí míchat', () => {
    const all = depthReading([wave(2, 1), wave(4, 2)])
    expect(all.inSigma).toBe(true)
    expect(all.value(wave(4, 2))).toBe(2)
    const mixed = depthReading([wave(2, 1), wave(4, null)])
    expect(mixed.inSigma).toBe(false)
    expect(mixed.value(wave(2, 1))).toBe(2) // fallback na surovou hloubku
  })

  it('prázdný vstup není σ (fallback raw) a stats nesou příznak', () => {
    expect(depthReading([]).inSigma).toBe(false)
    const stats = waveDirectionStats([wave(2, 1), wave(4, 2)], 'RiskOn')
    expect(stats.inSigma).toBe(true)
    expect(stats.meanDepth).toBe(1.5) // průměr v σ, ne surový
  })
})
