/** Testy automatických seance markerů (pevné UTC časy, jen uvnitř rozsahu dat). */
import { expect, test } from 'vitest'
import { autoSessions } from './sessions'

function minutes(startIso: string, count: number): string[] {
  const start = new Date(startIso).getTime()
  return Array.from({ length: count }, (_, i) => new Date(start + i * 60_000).toISOString())
}

test('markery se umístí na správné minuty a mimo rozsah se vynechají', () => {
  // 12:00–14:30 UTC (151 minut)
  const keys = minutes('2026-07-17T12:00:00Z', 151)
  const markers = autoSessions(keys)
  const byLabel = new Map(markers.map((m) => [m.label, m.minuteIdx]))
  expect(byLabel.get('US Pre')).toBe(0) // 12:00 = první minuta
  expect(byLabel.get('US Open')).toBe(90) // 13:30
  expect(byLabel.has('Tokio')).toBe(false) // před rozsahem
  expect(byLabel.has('US Close')).toBe(false) // po rozsahu
})

test('celodenní data mají plnou sadu, prázdná žádnou', () => {
  const keys = minutes('2026-07-17T00:00:00Z', 21 * 60)
  const markers = autoSessions(keys)
  // Seance padnoucí na tutéž minutu se slučují do jednoho popisku (ADR-0006)
  expect(markers.map((m) => m.label)).toEqual([
    'Sydney · Tokio',
    'Šanghaj',
    'Indie',
    'Sydney Cl · Tokio Cl',
    'Šanghaj Cl · Frankfurt · Londýn',
    'Indie Cl',
    'US Pre',
    'US Open',
    'Frankfurt Cl · Londýn Cl',
    'US Close',
  ])
  // Markery jsou seřazené v čase a sedí na správných minutách
  expect(markers.map((m) => m.minuteIdx)).toEqual([0, 90, 225, 360, 420, 600, 720, 810, 930, 1200])
  expect(autoSessions([])).toEqual([])
})
