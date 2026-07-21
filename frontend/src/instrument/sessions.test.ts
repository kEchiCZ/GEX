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

test('mimo letní čas se US a evropské seance posouvají o hodinu později (#159)', () => {
  // 15. 1. 2026: US i EU standardní čas → uložené letní časy +1 h; Asie beze změny
  const keys = minutes('2026-01-15T00:00:00Z', 22 * 60)
  const byLabel = new Map(autoSessions(keys).map((m) => [m.label, m.minuteIdx]))
  expect(byLabel.get('US Open')).toBe(14 * 60 + 30) // 13:30 EDT času → 14:30 UTC v zimě
  expect(byLabel.get('US Close')).toBe(21 * 60)
  // Frankfurt/Londýn v zimě 8:00 — v létě splývaly se Šanghaj Cl (7:00), v zimě už ne
  expect(byLabel.get('Šanghaj Cl')).toBe(7 * 60)
  expect(byLabel.get('Frankfurt · Londýn')).toBe(8 * 60)
  expect(byLabel.get('Frankfurt Cl · Londýn Cl')).toBe(16 * 60 + 30)
  expect(byLabel.get('Indie')).toBe(3 * 60 + 45) // bez DST, beze změny
})
