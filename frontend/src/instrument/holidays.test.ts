import { expect, test } from 'vitest'

import { easterSundayIso, isEarlyClose, isUsMarketHoliday, usMarketHolidays } from './holidays'
import { expirySettleUtc } from './expiry'
import { outsideUsRth } from './marketclock'
import { isTradingSessionIso, nextTradingSessionIso } from './tz'

test('svátky NYSE z pravidel — shodné s engine settle.us_market_holidays (#1308)', () => {
  expect([...usMarketHolidays(2026)].sort()).toEqual([
    '2026-01-01',
    '2026-01-19',
    '2026-02-16',
    '2026-04-03',
    '2026-05-25',
    '2026-06-19',
    '2026-07-03',
    '2026-09-07',
    '2026-11-26',
    '2026-12-25',
  ])
  // Přesuny 2027: Vánoce a Juneteenth v sobotu → pátek, 4. 7. v neděli → pondělí
  expect(isUsMarketHoliday('2027-12-24')).toBe(true)
  expect(isUsMarketHoliday('2027-06-18')).toBe(true)
  expect(isUsMarketHoliday('2027-07-05')).toBe(true)
  // Nový rok 2028 v sobotu se na pátek nepřesouvá
  expect(isUsMarketHoliday('2027-12-31')).toBe(false)
  expect(easterSundayIso(2026)).toBe('2026-04-05')
})

test('Thanksgiving a Vánoce nejsou obchodní den, zkrácená seance je', () => {
  expect(isTradingSessionIso('2026-11-26')).toBe(false)
  expect(isTradingSessionIso('2026-12-25')).toBe(false)
  expect(isTradingSessionIso('2026-11-27')).toBe(true)
  expect(nextTradingSessionIso('2026-11-26')).toBe('2026-11-27')
  expect(nextTradingSessionIso('2026-12-25')).toBe('2026-12-28')
  expect(isEarlyClose('2026-11-27')).toBe(true)
  expect(isEarlyClose('2026-12-24')).toBe(true)
  expect(isEarlyClose('2026-07-03')).toBe(false) // držený svátek
})

test('settle a RTH zkrácené seance končí ve 13:00 ET', () => {
  expect(expirySettleUtc('20261127', 'ES')?.toISOString()).toBe('2026-11-27T18:00:00.000Z')
  expect(expirySettleUtc('20261125', 'ES')?.toISOString()).toBe('2026-11-25T21:00:00.000Z')
  expect(outsideUsRth(new Date(Date.UTC(2026, 10, 27, 17, 59)))).toBe(false) // 12:59 ET
  expect(outsideUsRth(new Date(Date.UTC(2026, 10, 27, 18, 0)))).toBe(true) // 13:00 ET
  expect(outsideUsRth(new Date(Date.UTC(2026, 10, 26, 16, 0)))).toBe(true) // Thanksgiving
})
