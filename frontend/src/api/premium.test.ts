/** Rich/cheap prémie (#875): prahy, chybějící vstupy, texty. */
import { expect, test } from 'vitest'
import { premiumLabel, premiumReading, premiumTooltip } from './briefing'
import type { IvRankRow, VolRegimeRow } from './briefing'

function iv(percentile: number | null): IvRankRow[] {
  return [
    {
      session_date: '2026-08-26',
      symbol: 'ES',
      source: 'ibkr',
      iv: 0.15,
      iv_rank: 0.2,
      iv_percentile: percentile,
      sample: 244,
    } as IvRankRow,
  ]
}

function vol(percentile: number): VolRegimeRow {
  return {
    session_date: '2026-08-26',
    session_range: 60,
    percentile,
    bucket: 'normal',
    sample: 200,
  }
}

test('klasifikace rich/neutral/cheap s prahem ±20 p. b.', () => {
  expect(premiumReading(iv(0.85), vol(0.4))?.label).toBe('rich')
  expect(premiumReading(iv(0.1), vol(0.6))?.label).toBe('cheap')
  expect(premiumReading(iv(0.5), vol(0.4))?.label).toBe('neutral')
  // Hrana: přesně +0.2 už je rich
  expect(premiumReading(iv(0.6), vol(0.4))?.label).toBe('rich')
})

test('bez IVR nebo vol režimu žádný default (AC #875)', () => {
  expect(premiumReading([], vol(0.4))).toBeNull()
  expect(premiumReading(iv(null), vol(0.4))).toBeNull()
  expect(premiumReading(iv(0.5), null)).toBeNull()
})

test('texty řádku a odřádkovaný tooltip', () => {
  const rich = premiumReading(iv(0.85), vol(0.4))!
  expect(premiumLabel(rich)).toBe('rich: IV p85 vs. HV p40 — trh platí za hedge')
  const tooltip = premiumTooltip(rich)
  expect(tooltip).toContain('\n')
  expect(tooltip).toContain('+45 p. b.')
  expect(tooltip).toContain('• rich')
  const cheap = premiumReading(iv(0.1), vol(0.6))!
  expect(premiumLabel(cheap)).toContain('cheap')
})
