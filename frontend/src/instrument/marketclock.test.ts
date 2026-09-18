/** Hodiny US trhu (#206) — DST-korektně přes America/New_York. */
import { expect, test } from 'vitest'
import { newYorkClock, outsideUsRth } from './marketclock'

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
