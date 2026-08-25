/** Rozlišení velkých událostí pro týdenní výhled (#830). */
import { expect, test } from 'vitest'
import { isHighImpact } from './news'
import type { NewsRow } from './news'

function row(over: Partial<NewsRow>): NewsRow {
  return {
    id: 1,
    ts_event: '2026-08-26T12:30:00Z',
    ts_ingested: '2026-08-24T00:00:00Z',
    source: 'forexfactory',
    kind: 'scheduled',
    category: 'MACRO_INFLATION',
    importance: 1,
    title: 'USD Core PCE Price Index m/m',
    summary: null,
    sentiment_dir: null,
    sentiment_score: null,
    sentiment_source: null,
    forecast: null,
    previous: null,
    ...over,
  } as NewsRow
}

test('impact ze zdroje rozhoduje, importance je jen záložka (#830)', () => {
  // V produkčních datech má importance 3 i Low události a High se objevuje
  // s importance 1 — proto se čte `raw.impact`, když je k dispozici
  expect(isHighImpact(row({ raw: { impact: 'High' }, importance: 1 }))).toBe(true)
  expect(isHighImpact(row({ raw: { impact: 'Low' }, importance: 3 }))).toBe(false)
  expect(isHighImpact(row({ raw: { impact: 'Medium' }, importance: 3 }))).toBe(false)

  // Starší záznamy bez `raw` spadnou na importance
  expect(isHighImpact(row({ importance: 3 }))).toBe(true)
  expect(isHighImpact(row({ importance: 1 }))).toBe(false)
  expect(isHighImpact(row({ importance: null }))).toBe(false)
})
