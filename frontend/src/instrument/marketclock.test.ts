/** Hodiny US trhu (#206) — DST-korektně přes America/New_York. */
import { expect, test } from 'vitest'
import { feedSilenceMinutes, newYorkClock, outsideUsRth } from './marketclock'

test('RTH 9:30–16:00 ET, léto i zima, víkend zavřeno', () => {
  // Léto: 13:30 UTC = 9:30 ET (open), 20:00 UTC = 16:00 ET (close)
  expect(outsideUsRth(new Date('2026-09-18T13:29:00Z'))).toBe(true)
  expect(outsideUsRth(new Date('2026-09-18T13:30:00Z'))).toBe(false)
  expect(outsideUsRth(new Date('2026-09-18T19:59:00Z'))).toBe(false)
  expect(outsideUsRth(new Date('2026-09-18T20:00:00Z'))).toBe(true)
  expect(outsideUsRth(new Date('2026-09-18T21:09:00Z'))).toBe(true) // 23:09 CEST po close
  // Zima: 14:30 UTC = 9:30 ET
  expect(outsideUsRth(new Date('2026-12-15T14:29:00Z'))).toBe(true)
  expect(outsideUsRth(new Date('2026-12-15T14:30:00Z'))).toBe(false)
  // Sobota
  expect(outsideUsRth(new Date('2026-09-19T15:00:00Z'))).toBe(true)
  expect(newYorkClock(new Date('2026-09-18T13:30:00Z'))).toEqual({ minutes: 570, weekday: 5 })
})

test('ticho tasty streamu je porucha jen v RTH a až od 3 min (#1228)', () => {
  const rth = new Date('2026-09-18T15:00:00Z')
  expect(feedSilenceMinutes('2026-09-18T14:58:30Z', rth)).toBeNull()
  expect(feedSilenceMinutes('2026-09-18T14:57:00Z', rth)).toBe(3)
  expect(feedSilenceMinutes('2026-09-18T10:40:00Z', rth)).toBe(260)
  // Sobota 19. 9. 2026: hodiny ticha, žádný poplach
  expect(feedSilenceMinutes('2026-09-19T09:19:00Z', new Date('2026-09-19T14:00:00Z'))).toBeNull()
  expect(feedSilenceMinutes(undefined, rth)).toBeNull()
  expect(feedSilenceMinutes('nesmysl', rth)).toBeNull()
})
