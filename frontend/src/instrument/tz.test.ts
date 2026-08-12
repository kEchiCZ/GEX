/** Testy převodu burzovní čas → UTC (#511): letní i zimní období, přechody DST. */
import { expect, test } from 'vitest'
import { sessionDateIso, zonedTimeUtc } from './tz'

test('zonedTimeUtc: 16:00 New York — 20:00 UTC v létě, 21:00 v zimě', () => {
  expect(zonedTimeUtc('America/New_York', 2026, 7, 17, 16, 0)).toBe(Date.UTC(2026, 6, 17, 20, 0))
  expect(zonedTimeUtc('America/New_York', 2026, 1, 15, 16, 0)).toBe(Date.UTC(2026, 0, 15, 21, 0))
})

test('zonedTimeUtc: chicagský a evropský čas', () => {
  // 15:00 CT == settle (16:00 ET)
  expect(zonedTimeUtc('America/Chicago', 2026, 7, 17, 15, 0)).toBe(Date.UTC(2026, 6, 17, 20, 0))
  // Londýn 8:00: léto 7:00 UTC, zima 8:00 UTC
  expect(zonedTimeUtc('Europe/London', 2026, 7, 17, 8, 0)).toBe(Date.UTC(2026, 6, 17, 7, 0))
  expect(zonedTimeUtc('Europe/London', 2026, 1, 15, 8, 0)).toBe(Date.UTC(2026, 0, 15, 8, 0))
})

test('zonedTimeUtc: dny kolem přechodu US DST (8. 3. a 1. 11. 2026)', () => {
  expect(zonedTimeUtc('America/New_York', 2026, 3, 6, 16, 0)).toBe(Date.UTC(2026, 2, 6, 21, 0)) // EST
  expect(zonedTimeUtc('America/New_York', 2026, 3, 9, 16, 0)).toBe(Date.UTC(2026, 2, 9, 20, 0)) // EDT
  expect(zonedTimeUtc('America/New_York', 2026, 11, 2, 16, 0)).toBe(Date.UTC(2026, 10, 2, 21, 0)) // EST
})

test('zonedTimeUtc: zóny bez DST a jižní polokoule', () => {
  // Tokio +9 celý rok
  expect(zonedTimeUtc('Asia/Tokyo', 2026, 7, 17, 9, 0)).toBe(Date.UTC(2026, 6, 17, 0, 0))
  expect(zonedTimeUtc('Asia/Tokyo', 2026, 1, 15, 9, 0)).toBe(Date.UTC(2026, 0, 15, 0, 0))
  // Indie +5:30
  expect(zonedTimeUtc('Asia/Kolkata', 2026, 1, 15, 9, 15)).toBe(Date.UTC(2026, 0, 15, 3, 45))
  // Sydney: červenec AEST (+10), leden AEDT (+11) — obrácené sezóny
  expect(zonedTimeUtc('Australia/Sydney', 2026, 7, 17, 10, 0)).toBe(Date.UTC(2026, 6, 17, 0, 0))
  expect(zonedTimeUtc('Australia/Sydney', 2026, 1, 15, 10, 0)).toBe(Date.UTC(2026, 0, 14, 23, 0))
})

test('sessionDateIso: obchodní den = Globex seance (#512)', () => {
  // Pondělí 20. 7. 2026, CDT (UTC−5): open v neděli 22:00 UTC
  expect(sessionDateIso(Date.UTC(2026, 6, 19, 21, 59))).toBe('2026-07-19') // před openem: ještě neděle
  expect(sessionDateIso(Date.UTC(2026, 6, 19, 22, 0))).toBe('2026-07-20') // nedělní open → pondělní osa
  expect(sessionDateIso(Date.UTC(2026, 6, 20, 15, 0))).toBe('2026-07-20') // pondělní dopoledne
  expect(sessionDateIso(Date.UTC(2026, 6, 20, 22, 30))).toBe('2026-07-21') // pondělní večer → úterní osa
  // Půlnoc UTC uprostřed seance nic nemění (19:00 CT = seance běží)
  expect(sessionDateIso(Date.UTC(2026, 6, 21, 0, 30))).toBe('2026-07-21')
  // Zima (CST, UTC−6): open ve 23:00 UTC
  expect(sessionDateIso(Date.UTC(2026, 0, 19, 22, 30))).toBe('2026-01-19')
  expect(sessionDateIso(Date.UTC(2026, 0, 19, 23, 0))).toBe('2026-01-20')
})
