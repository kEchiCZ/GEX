/** ⌛ značky kalendáře expirací v ose (#1189). */
import { describe, expect, it } from 'vitest'
import { axisDatesOf, buildExpiryMarkers } from './expiryMarkers'
import type { CalendarMarkerRow } from '../api/calendar'

const MARKERS: CalendarMarkerRow[] = [
  { date: '2026-09-10', kind: 'roll', ts: '2026-09-10T13:30:00+00:00', label: 'Roll' },
  { date: '2026-09-16', kind: 'vix_expiry', ts: '2026-09-16T13:30:00+00:00', label: 'VIX' },
  { date: '2026-09-18', kind: 'quarterly_expiry', ts: '2026-09-18T13:30:00+00:00', label: 'SOQ' },
]

const timeLabel = (iso: string) => iso.slice(11, 16)
const dayLabel = (iso: string) => iso.slice(0, 10)

describe('buildExpiryMarkers', () => {
  it('intraday: páruje jen značky ze dnů osy, čas stejným formatterem', () => {
    const labels = ['13:29', '13:30', '13:31']
    const axis = axisDatesOf(['2026-09-18T13:29:00Z', '2026-09-18T13:31:00Z'])
    const result = buildExpiryMarkers(MARKERS, labels, timeLabel, axis)
    expect(result).toEqual([{ minuteIdx: 1, kind: 'quarterly_expiry', label: 'SOQ', major: true }])
    // Jiný den: VIX středy neskočí na páteční osu a naopak
    const wednesday = buildExpiryMarkers(
      MARKERS,
      labels,
      timeLabel,
      axisDatesOf(['2026-09-16T13:30:00Z']),
    )
    expect(wednesday.map((m) => m.kind)).toEqual(['vix_expiry'])
    expect(wednesday[0].major).toBe(false)
  })

  it('daily: páruje datem přes celou osu, bez filtru dnů', () => {
    const labels = ['2026-09-09', '2026-09-10', '2026-09-18']
    const result = buildExpiryMarkers(MARKERS, labels, dayLabel, null)
    expect(result.map((m) => [m.minuteIdx, m.kind])).toEqual([
      [1, 'roll'],
      [2, 'quarterly_expiry'],
    ])
    expect(buildExpiryMarkers(MARKERS, [], dayLabel, null)).toEqual([])
  })
})
