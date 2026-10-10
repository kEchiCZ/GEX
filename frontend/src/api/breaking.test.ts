/** Pomocníci karty Breaking news (E-6.28c). */
import { expect, test } from 'vitest'
import {
  delayLabel,
  excursionLabel,
  formatBp,
  groupLabel,
  impactStateLabel,
  impactTooltip,
  themeLabel,
} from './breaking'
import type { BreakingImpact } from './breaking'

const RUNNING: BreakingImpact = {
  state: 'running',
  elapsed_min: 2,
  ret_bp: 12.34,
  range_bp: 15,
  excursion_bp: 8.06,
  excursion_direction: -1,
  contaminated: false,
}

test('stav dopadu: běží, zafixováno, trh zavřený, bez dat', () => {
  expect(impactStateLabel(RUNNING)).toBe('běží 2 min')
  expect(impactStateLabel({ ...RUNNING, state: 'fixed' })).toBe('5 min')
  expect(impactStateLabel({ ...RUNNING, state: 'closed' })).toBe('trh zavřený')
  expect(impactStateLabel({ ...RUNNING, state: 'no_data' })).toBe('bez dat')
})

test('bp se znaménkem, výchylka se směrem, pomlčka bez hodnoty', () => {
  expect(formatBp(12.34)).toBe('+12.3 bp')
  expect(formatBp(-0.04)).toBe('-0.0 bp')
  expect(formatBp(null)).toBe('—')
  expect(excursionLabel(RUNNING)).toBe('↓ 8.1 bp')
  expect(excursionLabel({ ...RUNNING, excursion_bp: null })).toBeNull()
})

test('zpoždění zdroje v s, min a h', () => {
  expect(delayLabel(1)).toBe('+1 s')
  expect(delayLabel(390)).toBe('+7 min')
  expect(delayLabel(40_000)).toBe('+11 h')
})

test('české popisky skupin a témat, neznámý klíč syrově', () => {
  expect(groupLabel('central_banks')).toBe('Centrální banky')
  expect(themeLabel('middle_east')).toBe('Blízký východ')
  expect(themeLabel('tariffs')).toBe('Cla')
  expect(themeLabel(null)).toBeNull()
  expect(themeLabel('novy')).toBe('novy')
})

test('tooltip dopadu jsou odrážky pod sebou, kontaminace s ⚠', () => {
  const tooltip = impactTooltip('ES', { ...RUNNING, contaminated: true })
  const lines = tooltip.split('\n')
  expect(lines[0]).toContain('ES')
  expect(lines.slice(1).every((line) => line.startsWith('•'))).toBe(true)
  expect(tooltip).toContain('⚠')
  expect(impactTooltip('NQ', { ...RUNNING, state: 'closed' })).toContain('zavřeném trhu')
})
