/** ⌛ značky kalendáře expirací v ose (#1189, #1303). */
import { describe, expect, it } from 'vitest'
import { buildExpiryMarkers } from './expiryMarkers'
import { dailyAxisLocator, sessionAxisLocator } from './axisLocator'
import { segmentOf } from '../test/axisSegment'
import type { CalendarMarkerRow } from '../api/calendar'

const MARKERS: CalendarMarkerRow[] = [
  { date: '2026-09-10', kind: 'roll', ts: '2026-09-10T13:30:00+00:00', label: 'Roll' },
  { date: '2026-09-16', kind: 'vix_expiry', ts: '2026-09-16T13:30:00+00:00', label: 'VIX' },
  { date: '2026-09-18', kind: 'quarterly_expiry', ts: '2026-09-18T13:30:00+00:00', label: 'SOQ' },
]

describe('buildExpiryMarkers', () => {
  it('intraday: jen značky seance osy, časem do koše', () => {
    const friday = sessionAxisLocator([segmentOf('2026-09-18T13:29:00Z', 3)], Infinity)
    const result = buildExpiryMarkers(MARKERS, friday)
    expect(result).toEqual([{ minuteIdx: 1, kind: 'quarterly_expiry', label: 'SOQ', major: true }])
    // Jiný den: VIX středy neskočí na páteční osu a naopak
    const wednesday = buildExpiryMarkers(
      MARKERS,
      sessionAxisLocator([segmentOf('2026-09-16T13:30:00Z', 1)], Infinity),
    )
    expect(wednesday.map((m) => m.kind)).toEqual(['vix_expiry'])
    expect(wednesday[0].major).toBe(false)
  })

  it('15m: SOQ 13:30 padne do koše 13:15–13:30 (#1303, dřív zmizel)', () => {
    const locate = sessionAxisLocator([segmentOf('2026-09-18T13:15:00Z', 4, 15)], Infinity)
    expect(buildExpiryMarkers(MARKERS, locate).map((m) => m.minuteIdx)).toEqual([1])
  })

  it('daily: páruje datem přes celou osu', () => {
    const labels = ['2026-09-09', '2026-09-10', '2026-09-18']
    const result = buildExpiryMarkers(
      MARKERS,
      dailyAxisLocator(labels, (date) => date),
    )
    expect(result.map((m) => [m.minuteIdx, m.kind])).toEqual([
      [1, 'roll'],
      [2, 'quarterly_expiry'],
    ])
    expect(
      buildExpiryMarkers(
        MARKERS,
        dailyAxisLocator([], (date) => date),
      ),
    ).toEqual([])
  })
})
