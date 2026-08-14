/** Testy range selectoru (#484): okenní profil = parita s API aritmetikou (#483). */
import { describe, expect, it } from 'vitest'
import {
  decodeRange,
  encodeRange,
  minuteIndexFor,
  rangeBuckets,
  followUpRange,
  prePostWindows,
  rangeLabel,
  reactionWindow,
  windowCumDelta,
  windowProfileRows,
} from './rangeselect'
import type { ProfileRow } from '../profile/bars'

/** Řádek jako z profileByMinute: kumulativní volume, component = vol × |Δ|. */
function row(strike: number, callVol: number, putVol: number): ProfileRow {
  return {
    strike,
    callVolume: callVol,
    putVolume: putVol,
    callVolComponent: callVol * 0.5,
    putVolComponent: putVol * 0.4,
    callOiComponent: 50,
    putOiComponent: 60,
    callOi: 100,
    putOi: 150,
    distanceFromSpot: -11,
  }
}

describe('windowProfileRows', () => {
  it('okno = diff kumulativů, |Δ| k t2 se zachová, OI složky statické', () => {
    // Parita s API (#483): fixture vzor — vol(t) = 10·(t+1) per strana
    const rows1 = [row(7590, 15, 10)]
    const rows2 = [row(7590, 35, 30)]
    const result = windowProfileRows(rows2, rows1)
    expect(result[0].callVolume).toBe(20)
    expect(result[0].putVolume).toBe(20)
    expect(result[0].callVolComponent).toBeCloseTo(20 * 0.5, 10)
    expect(result[0].putVolComponent).toBeCloseTo(20 * 0.4, 10)
    expect(result[0].callOiComponent).toBe(50) // OI statické k t2
    expect(result[0].callOi).toBe(100)
  })

  it('prázdná baseline = okno od začátku dat (== plný kumulativ)', () => {
    const rows2 = [row(7590, 35, 30)]
    const result = windowProfileRows(rows2, [])
    expect(result[0].callVolume).toBe(35)
    expect(result[0].callVolComponent).toBeCloseTo(35 * 0.5, 10)
  })

  it('korekce dat (kumulativ klesl) se clampuje na 0', () => {
    const result = windowProfileRows([row(7590, 10, 10)], [row(7590, 15, 10)])
    expect(result[0].callVolume).toBe(0)
    expect(result[0].callVolComponent).toBe(0)
    expect(result[0].putVolume).toBe(0)
  })

  it('strike bez baseline řádku (přibyl posunem obálky) má baseline 0', () => {
    const result = windowProfileRows([row(7600, 12, 8)], [row(7590, 15, 10)])
    expect(result[0].callVolume).toBe(12)
  })
})

describe('minuteIndexFor / rangeBuckets', () => {
  const minutes = [
    '2026-08-13T15:00:00Z',
    '2026-08-13T15:01:00Z',
    '2026-08-13T15:03:00Z', // díra v ose (#502)
    '2026-08-13T15:04:00Z',
  ]

  it('nejbližší minuta ≤ iso, díry nevadí', () => {
    expect(minuteIndexFor(minutes, '2026-08-13T15:01:00Z')).toBe(1)
    expect(minuteIndexFor(minutes, '2026-08-13T15:02:00Z')).toBe(1)
    expect(minuteIndexFor(minutes, '2026-08-13T14:00:00Z')).toBeNull()
  })

  it('koše dle TF', () => {
    const buckets = rangeBuckets(
      { fromIso: '2026-08-13T15:01:00Z', toIso: '2026-08-13T15:04:00Z' },
      minutes,
      2,
    )
    expect(buckets).toEqual({ fromIdx: 1, toIdx: 3, startBucket: 0, endBucket: 1 })
  })
})

describe('windowCumDelta', () => {
  it('diff posledních hodnot ≤ hranic — kotva open se odečte', () => {
    const series = [
      { tsIso: '2026-08-13T15:00:00Z', cumDelta: 50 },
      { tsIso: '2026-08-13T15:01:00Z', cumDelta: 100 },
      { tsIso: '2026-08-13T15:02:00Z', cumDelta: 150 },
    ]
    const value = windowCumDelta(series, {
      fromIso: '2026-08-13T15:00:00Z',
      toIso: '2026-08-13T15:02:00Z',
    })
    expect(value).toBe(100) // 150 − 50, parita s /flow window (#483)
  })

  it('prázdná řada → null', () => {
    expect(windowCumDelta([], { fromIso: 'a', toIso: 'b' })).toBeNull()
  })
})

describe('URL round-trip', () => {
  it('encode/decode a odmítnutí nesmyslů', () => {
    const range = { fromIso: '2026-08-13T15:00:00Z', toIso: '2026-08-13T15:30:00Z' }
    expect(decodeRange(encodeRange(range))).toEqual(range)
    expect(decodeRange(null)).toBeNull()
    expect(decodeRange('nesmysl')).toBeNull()
    expect(decodeRange('2026-08-13T15:30:00Z~2026-08-13T15:00:00Z')).toBeNull() // to < from
  })

  it('rangeLabel formátuje HH:MM–HH:MM', () => {
    expect(rangeLabel({ fromIso: '2026-08-13T15:00:00Z', toIso: '2026-08-13T15:30:00Z' })).toMatch(
      /^\d{2}:\d{2}–\d{2}:\d{2}$/,
    )
  })
})

describe('reactionWindow (#488)', () => {
  const event = '2026-08-13T14:30:00Z'

  it('uzavřené okno: ts_event → +15 min', () => {
    const result = reactionWindow(event, 15, '2026-08-13T16:00:00Z')
    expect(result).toEqual({
      range: { fromIso: '2026-08-13T14:30:00.000Z', toIso: '2026-08-13T14:45:00.000Z' },
      open: false,
    })
  })

  it('běžící okno se clampne na živou hranu a označí open', () => {
    const result = reactionWindow(event, 60, '2026-08-13T15:00:00Z')
    expect(result?.range.toIso).toBe('2026-08-13T15:00:00.000Z')
    expect(result?.open).toBe(true)
  })

  it('event za hranou dat / nevalidní vstup → null', () => {
    expect(reactionWindow(event, 15, '2026-08-13T14:00:00Z')).toBeNull() // data končí před eventem
    expect(reactionWindow(event, 15, null)).toBeNull()
    expect(reactionWindow('nesmysl', 15, '2026-08-13T16:00:00Z')).toBeNull()
  })
})

describe('duální rozsah (#489)', () => {
  it('followUpRange: B stejné šířky hned za A, clamp na data', () => {
    const a = { fromIso: '2026-08-14T14:00:00.000Z', toIso: '2026-08-14T14:29:00.000Z' }
    expect(followUpRange(a, '2026-08-14T16:00:00Z')).toEqual({
      fromIso: '2026-08-14T14:30:00.000Z',
      toIso: '2026-08-14T14:59:00.000Z',
    })
    // Clamp: za A je jen 10 minut dat
    expect(followUpRange(a, '2026-08-14T14:40:00Z')?.toIso).toBe('2026-08-14T14:40:00.000Z')
    // Za A žádná data → null
    expect(followUpRange(a, '2026-08-14T14:29:00Z')).toBeNull()
  })

  it('prePostWindows: A končí minutu před eventem, B = reakční okno', () => {
    const result = prePostWindows('2026-08-14T14:30:00Z', 15, '2026-08-14T16:00:00Z')
    expect(result).toEqual({
      a: { fromIso: '2026-08-14T14:15:00.000Z', toIso: '2026-08-14T14:29:00.000Z' },
      b: { fromIso: '2026-08-14T14:30:00.000Z', toIso: '2026-08-14T14:45:00.000Z' },
      bOpen: false,
    })
    const open = prePostWindows('2026-08-14T14:30:00Z', 60, '2026-08-14T15:00:00Z')
    expect(open?.bOpen).toBe(true)
  })
})
