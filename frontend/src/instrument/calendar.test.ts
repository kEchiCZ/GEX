/** Testy mřížky kalendáře expirací (#513). */
import { expect, test } from 'vitest'
import { dayTitle, expiryKeyOf, expiryMonth, kindClass, monthGrid, monthLabel } from './calendar'

test('monthGrid: srpen 2026 začíná pondělím 27. 7. a má 6 týdnů', () => {
  const weeks = monthGrid(2026, 7, new Set())
  expect(weeks).toHaveLength(6)
  expect(weeks[0][0]).toEqual({ expiry: null, day: 27, inMonth: false })
  // 1. 8. 2026 je sobota — šestý den prvního týdne
  expect(weeks[0][5]).toEqual({ expiry: null, day: 1, inMonth: true })
  expect(weeks[5][6]).toEqual({ expiry: null, day: 6, inMonth: false })
})

test('monthGrid: expirační dny nesou klíč, ostatní null', () => {
  const weeks = monthGrid(2026, 7, new Set(['20260828', '20260901']))
  const days = weeks.flat()
  expect(days.find((d) => d.day === 28 && d.inMonth)?.expiry).toBe('20260828')
  // 1. 9. je v mřížce srpna jako přesah — expirace se značí i tam
  expect(days.find((d) => d.day === 1 && !d.inMonth && d.expiry !== null)?.expiry).toBe('20260901')
  expect(days.find((d) => d.day === 27 && d.inMonth)?.expiry).toBeNull()
})

test('expiryKeyOf + expiryMonth: tam a zpět', () => {
  expect(expiryKeyOf(new Date(Date.UTC(2026, 7, 5)))).toBe('20260805')
  expect(expiryMonth('20260805')).toEqual({ year: 2026, month: 7 })
  expect(expiryMonth('nesmysl')).toBeNull()
})

test('monthLabel: české názvy měsíců', () => {
  expect(monthLabel(2026, 7)).toBe('srpen 2026')
  expect(monthLabel(2027, 0)).toBe('leden 2027')
})

test('kindClass: druh → ASCII CSS třída', () => {
  expect(kindClass('denní')).toBe('cal-kind-daily')
  expect(kindClass('kvartální')).toBe('cal-kind-quarterly')
  expect(kindClass(null)).toBe('')
})

test('dayTitle: druh + série + zdroj tasty', () => {
  // 28. 8. 2026 = pátek (ne třetí, ne poslední obchodní den) → týdenní
  expect(dayTitle('20260828', ['EW4'], false)).toBe('týdenní · EW4')
  expect(dayTitle('20260827', [], true)).toBe('denní · zdroj tastytrade')
  // 31. 8. 2026 = pondělí, poslední obchodní den měsíce → EOM
  expect(dayTitle('20260831', ['EW'], false)).toBe('EOM · EW')
})
