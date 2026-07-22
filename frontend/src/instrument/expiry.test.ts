/** Testy klasifikace expirací a odpočtu (kalendářní pravidla CME řetězu). */
import { expect, test } from 'vitest'
import { frontContractCode } from './expiry'

test('frontContractCode: TWS symbol předního kvartálního kontraktu (#189)', () => {
  // Červenec 2026 → září (3. pátek 18. 9. 2026 je v budoucnu) → ESU6
  expect(frontContractCode('ES', new Date('2026-07-22T09:00:00Z'))).toBe('ESU6')
  expect(frontContractCode('NQ', new Date('2026-07-22T09:00:00Z'))).toBe('NQU6')
  // V den zářijové expirace se kód přepne na prosinec
  expect(frontContractCode('ES', new Date('2026-09-18T10:00:00Z'))).toBe('ESZ6')
  // Po prosincové expiraci (18. 12. 2026) → březen dalšího roku
  expect(frontContractCode('ES', new Date('2026-12-20T00:00:00Z'))).toBe('ESH7')
  // Nekvartální produkt (měsíční cyklus) neodhadujeme
  expect(frontContractCode('CL', new Date('2026-07-22T09:00:00Z'))).toBeNull()
})
import { expiryCountdown, expiryKind } from './expiry'

test('expiryKind: 3. pátek = měsíční, v kvartálních měsících kvartální', () => {
  expect(expiryKind('20260717')).toBe('měsíční') // 3. pátek července (dnešní opex)
  expect(expiryKind('20260918')).toBe('kvartální') // 3. pátek září
  expect(expiryKind('20261218')).toBe('kvartální')
})

test('expiryKind: pátek = týdenní, poslední obchodní den = EOM, jinak denní', () => {
  expect(expiryKind('20260724')).toBe('týdenní') // 4. pátek
  expect(expiryKind('20260731')).toBe('EOM') // pátek a zároveň konec měsíce → EOM
  expect(expiryKind('20260720')).toBe('denní') // pondělí
  expect(expiryKind('20260721')).toBe('denní') // úterý
  expect(expiryKind('nesmysl')).toBeNull()
})

test('expiryCountdown: odpočet k ≈20:00 UTC, po expiraci null', () => {
  const now = new Date(Date.UTC(2026, 6, 17, 14, 18)) // 14:18 UTC v den expirace
  expect(expiryCountdown('20260717', now)).toBe('≈ za 5 h 42 m')
  expect(expiryCountdown('20260717', new Date(Date.UTC(2026, 6, 17, 21, 0)))).toBeNull()
  expect(expiryCountdown('20260720', now)).toBe('≈ za 3 d')
  expect(expiryCountdown('20260717', new Date(Date.UTC(2026, 6, 17, 19, 30)))).toBe('≈ za 30 m')
})
