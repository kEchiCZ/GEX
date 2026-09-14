/** Testy statistiky korekčních epizod (#565) — čisté funkce. */
import { describe, expect, it } from 'vitest'
import { episodeLabelText, episodeStats, recentEpisodes } from './episodes'
import type { EpisodeRow } from '../api/news'

function episode(overrides: Partial<EpisodeRow>): EpisodeRow {
  return {
    id: 1,
    symbol: 'ES',
    start_date: '2026-08-05',
    end_date: '2026-08-19',
    ref_level_z: 3.86,
    depth_z: 6.78,
    label: 'negation',
    length_days: 10,
    params_version: 1,
    series_variant: 'zscore_100',
    ...overrides,
  }
}

describe('episodeStats', () => {
  it('počítá třídy, průměry a předběžnost pod 20 rozhodnutými', () => {
    const rows = [
      episode({ id: 1 }),
      episode({ id: 2, label: 'attempt', depth_z: 3.41, length_days: 10, end_date: '2026-09-11' }),
      episode({ id: 3, label: 'attempt', depth_z: 1.41, length_days: 2 }),
      episode({ id: 4, label: null, end_date: null, length_days: 1 }),
    ]
    const stats = episodeStats(rows)
    expect(stats.attempts.count).toBe(2)
    expect(stats.attempts.meanDepth).toBeCloseTo(2.41, 6)
    expect(stats.attempts.meanLength).toBe(6)
    expect(stats.negations.count).toBe(1)
    expect(stats.open).toBe(1)
    expect(stats.resolved).toBe(3)
    expect(stats.preliminary).toBe(true)
    expect(stats.paramsVersion).toBe(1)
  })

  it('prázdný vstup a smíchané verze parametrů', () => {
    expect(episodeStats([]).paramsVersion).toBeNull()
    expect(
      episodeStats([episode({ id: 1 }), episode({ id: 2, params_version: 2 })]).paramsVersion,
    ).toBe(-1)
  })

  it('20 rozhodnutých už není předběžné', () => {
    const rows = Array.from({ length: 20 }, (_, index) =>
      episode({ id: index, label: index % 2 ? 'attempt' : 'negation' }),
    )
    expect(episodeStats(rows).preliminary).toBe(false)
  })
})

describe('recentEpisodes a popisky', () => {
  it('řadí nejnovější první a ořízne', () => {
    const rows = [
      episode({ id: 1, start_date: '2026-08-05' }),
      episode({ id: 2, start_date: '2026-09-08' }),
      episode({ id: 3, start_date: '2026-08-28' }),
    ]
    expect(recentEpisodes(rows, 2).map((row) => row.id)).toEqual([2, 3])
  })

  it('popisky tříd česky', () => {
    expect(episodeLabelText('attempt')).toBe('pokus')
    expect(episodeLabelText('negation')).toBe('negace')
    expect(episodeLabelText(null)).toBe('probíhá')
  })
})
