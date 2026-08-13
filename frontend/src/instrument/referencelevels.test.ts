/** Testy referenčních úrovní (#678): ON okno, PD extrémy, VWAP a mapování na osu. */
import { describe, expect, it } from 'vitest'
import { computeReferenceLevels, vwapSeriesForAxis } from './referencelevels'
import type { BarRow } from '../api/briefing'

const OPEN_MS = Date.parse('2026-08-13T13:30:00Z')

function bar(ts: string, high: number, low: number, close: number, volume = 100): BarRow {
  return { ts_min: ts, open: close, high, low, close, volume }
}

describe('computeReferenceLevels', () => {
  const todayBars = [
    bar('2026-08-13T04:00:00Z', 6410, 6390, 6400),
    bar('2026-08-13T10:00:00Z', 6425, 6405, 6420),
    bar('2026-08-13T14:00:00Z', 6460, 6415, 6455), // po openu — do ON nepatří
  ]
  const prevDayBars = [
    bar('2026-08-12T10:00:00Z', 6395, 6360, 6380),
    bar('2026-08-12T15:00:00Z', 6405, 6370, 6402),
  ]

  it('ONH/ONL jen z barů před US openem, PD extrémy + close z celé seance', () => {
    const levels = computeReferenceLevels({
      todayBars,
      prevDayBars,
      usOpenMs: OPEN_MS,
      nowMs: Date.parse('2026-08-13T14:30:00Z'),
    })
    expect(levels.onHigh).toBe(6425)
    expect(levels.onLow).toBe(6390)
    expect(levels.onRunning).toBe(false)
    expect(levels.prevHigh).toBe(6405)
    expect(levels.prevLow).toBe(6360)
    expect(levels.prevClose).toBe(6402)
  })

  it('před openem je ON okno označené jako běžící', () => {
    const levels = computeReferenceLevels({
      todayBars: todayBars.slice(0, 2),
      prevDayBars,
      usOpenMs: OPEN_MS,
      nowMs: Date.parse('2026-08-13T11:00:00Z'),
    })
    expect(levels.onRunning).toBe(true)
    expect(levels.onHigh).toBe(6425)
  })

  it('VWAP = kumulativní typická cena × objem / objem', () => {
    const levels = computeReferenceLevels({
      todayBars: [
        bar('2026-08-13T10:00:00Z', 6410, 6390, 6400, 100), // typical 6400
        bar('2026-08-13T10:01:00Z', 6430, 6410, 6420, 300), // typical 6420
      ],
      prevDayBars: [],
      usOpenMs: OPEN_MS,
      nowMs: Date.parse('2026-08-13T11:00:00Z'),
    })
    expect(levels.vwap).toHaveLength(2)
    expect(levels.vwap[0].value).toBe(6400)
    expect(levels.vwap[1].value).toBe(6415) // (6400·100 + 6420·300) / 400
    expect(levels.prevHigh).toBeNull()
  })
})

describe('vwapSeriesForAxis', () => {
  it('mapuje přes ISO čas a drží poslední hodnotu, agregace po koších', () => {
    const minutesIso = [
      '2026-08-13T10:00:00Z',
      '2026-08-13T10:01:00Z',
      '2026-08-13T10:02:00Z',
      '2026-08-13T10:03:00Z',
    ]
    const vwap = [
      { tsIso: '2026-08-13T10:00:00Z', value: 6400 },
      { tsIso: '2026-08-13T10:02:00Z', value: 6410 },
    ]
    expect(vwapSeriesForAxis(vwap, minutesIso, 1)).toEqual([6400, 6400, 6410, 6410])
    expect(vwapSeriesForAxis(vwap, minutesIso, 2)).toEqual([6400, 6410])
  })

  it('minuty před prvním VWAP bodem zůstávají null', () => {
    const minutesIso = ['2026-08-13T09:59:00Z', '2026-08-13T10:00:00Z']
    const vwap = [{ tsIso: '2026-08-13T10:00:00Z', value: 6400 }]
    expect(vwapSeriesForAxis(vwap, minutesIso, 1)).toEqual([null, 6400])
  })
})
