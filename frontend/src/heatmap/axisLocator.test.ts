/** Poloha značek na ose časem (#1303) — vzor news markerů #1290. */
import { describe, expect, it } from 'vitest'
import { dailyAxisLocator, sessionAxisLocator } from './axisLocator'
import { segmentOf } from '../test/axisSegment'

describe('sessionAxisLocator', () => {
  it('na 5m padne okamžik do koše, který ho obsahuje (dřív zmizel mimo hranici)', () => {
    const locate = sessionAxisLocator([segmentOf('2026-08-13T15:00:00Z', 6, 5)], Infinity)
    expect(locate('2026-08-13T15:07:00Z')).toBe(1)
    expect(locate('2026-08-13T15:24:59Z')).toBe(4)
  })

  it('pauza CME bez koše: minulý okamžik se přimkne k poslednímu koši před ním', () => {
    // Koše do 20:59 (15:59 CDT), seance pokračuje až do 22:00 UTC otevření
    const segment = segmentOf('2026-08-13T20:55:00Z', 5, 1, {
      openIso: '2026-08-12T22:00:00Z',
      closeIso: '2026-08-13T22:00:00Z',
    })
    const locate = sessionAxisLocator([segment], Infinity)
    expect(locate('2026-08-13T21:30:00Z')).toBe(4)
  })

  it('budoucí okamžik jen do koše projekce, mimo seanci nic', () => {
    const segment = segmentOf('2026-08-13T15:00:00Z', 10)
    const now = Date.parse('2026-08-13T15:04:30Z')
    const locate = sessionAxisLocator([segment], now)
    expect(locate('2026-08-13T15:08:00Z')).toBe(8)
    expect(locate('2026-08-14T15:00:00Z')).toBeNull()
    expect(locate('nesmysl')).toBeNull()
    expect(sessionAxisLocator([], now)('2026-08-13T15:01:00Z')).toBeNull()
  })
})

describe('dailyAxisLocator', () => {
  it('páruje datem sloupce', () => {
    const locate = dailyAxisLocator(['2026-09-09', '2026-09-10'], (date) => date)
    expect(locate('2026-09-10T13:30:00+00:00')).toBe(1)
    expect(locate('2026-09-11T13:30:00+00:00')).toBeNull()
  })
})
