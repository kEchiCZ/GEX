/** Testy pokrytí dat a čerstvosti (#470). */
import { expect, test } from 'vitest'
import {
  coverageLabel,
  dataAgeMinutes,
  greeksCoverage,
  ohlcCoverage,
  STALE_AFTER_MINUTES,
} from './coverage'

const iso = (minute: number): string =>
  new Date(Date.parse('2026-08-10T13:00:00.000Z') + minute * 60_000).toISOString()

test('ohlcCoverage měří proti ČASOVÉMU rozpětí osy, takže díru odhalí', () => {
  // Osa: minuty 0–9 souvisle, pak výpadek 45 min, pak 55–59 → 15 sloupců, rozpětí 60 minut
  const axis = [...Array.from({ length: 10 }, (_, i) => iso(i)), ...Array.from({ length: 5 }, (_, i) => iso(55 + i))] // prettier-ignore
  const bars = axis.map((_, index) => index) // každý sloupec osy má bar
  const coverage = ohlcCoverage(axis, bars)!
  expect(coverage.covered).toBe(15)
  expect(coverage.expected).toBe(60) // 13:00 → 13:59
  expect(coverage.ratio).toBeCloseTo(0.25, 5)
  expect(coverageLabel(coverage)).toBe('15/60 (25 %)')
})

test('ohlcCoverage: souvislý den bez děr je 100 %, duplikáty barů se nepočítají dvakrát', () => {
  const axis = Array.from({ length: 30 }, (_, i) => iso(i))
  const full = ohlcCoverage(axis, [...axis.keys()])!
  expect(full).toMatchObject({ covered: 30, expected: 30 })
  expect(full.ratio).toBe(1)
  // Provizorní i finální bar téže minuty = jedna pokrytá minuta
  const withDuplicates = ohlcCoverage(axis, [0, 0, 1, 1, 2])!
  expect(withDuplicates.covered).toBe(3)
})

test('ohlcCoverage: neměřitelná osa vrací null (demo den, Daily, nečitelné časy)', () => {
  expect(ohlcCoverage([], [])).toBeNull()
  expect(ohlcCoverage([iso(0)], [0])).toBeNull() // jediná minuta = není rozpětí
  expect(ohlcCoverage(['nesmysl', 'taky ne'], [0, 1])).toBeNull()
})

test('greeksCoverage: bez hodnot ze statusu null, jinak podíl', () => {
  expect(greeksCoverage(undefined, undefined)).toBeNull()
  expect(greeksCoverage(182, 0)).toBeNull() // dělení nulou
  const partial = greeksCoverage(91, 182)!
  expect(partial.ratio).toBeCloseTo(0.5, 5)
  expect(coverageLabel(partial)).toBe('91/182 (50 %)')
  // Přestřelený počet (engine dohlásí víc) se nezobrazí jako 120 %
  expect(greeksCoverage(200, 182)!.ratio).toBe(1)
})

test('dataAgeMinutes: stáří v minutách, budoucí čas nedává záporné', () => {
  const now = new Date('2026-08-10T13:10:00.000Z')
  expect(dataAgeMinutes('2026-08-10T13:08:00.000Z', now)).toBeCloseTo(2, 5)
  expect(dataAgeMinutes('2026-08-10T13:12:00.000Z', now)).toBe(0)
  expect(dataAgeMinutes(null, now)).toBeNull()
  expect(dataAgeMinutes('nesmysl', now)).toBeNull()
  // Hranice „stojí to" — sweep jede po minutě, dvě minuty jsou ještě provoz
  expect(dataAgeMinutes('2026-08-10T13:08:00.000Z', now)! >= STALE_AFTER_MINUTES).toBe(false)
  expect(dataAgeMinutes('2026-08-10T13:05:00.000Z', now)! >= STALE_AFTER_MINUTES).toBe(true)
})
