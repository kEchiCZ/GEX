/** Testy range selectoru (#484): okenní profil = parita s API aritmetikou (#483). */
import { describe, expect, it } from 'vitest'
import {
  panelWindowCumDelta,
  decodeRange,
  encodeRange,
  minuteIndexFor,
  rangeBuckets,
  followUpRange,
  prePostWindows,
  rangeLabel,
  reactionWindow,
  windowProfileRows,
  windowVolumes,
} from './rangeselect'
import type { WindowVolumes } from './rangeselect'
import type { RawDay } from '../heatmap/modes'
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
  const volumes = (entries: [number, number, number][]): WindowVolumes =>
    new Map(entries.map(([strike, call, put]) => [strike, { call, put }]))

  it('objem okna z windowVolumes, |Δ| k t2 se zachová, OI složky statické', () => {
    const rows2 = [row(7590, 35, 30)]
    const result = windowProfileRows(rows2, volumes([[7590, 20, 20]]))
    expect(result[0].callVolume).toBe(20)
    expect(result[0].putVolume).toBe(20)
    expect(result[0].callVolComponent).toBeCloseTo(20 * 0.5, 10)
    expect(result[0].putVolComponent).toBeCloseTo(20 * 0.4, 10)
    expect(result[0].callOiComponent).toBe(50) // OI statické k t2
    expect(result[0].callOi).toBe(100)
  })

  it('strike bez objemu okna má 0', () => {
    const result = windowProfileRows([row(7600, 12, 8)], volumes([[7590, 15, 10]]))
    expect(result[0].callVolume).toBe(0)
    expect(result[0].callVolComponent).toBe(0)
  })
})

/** Jeden strike 7590, call = `series`, put prázdný; `null` = volume NULL
 * (maska 0), `undefined` = minuta bez snapshotu. */
function rawOf(series: (number | null | undefined)[]): RawDay {
  const minutes = series.length
  const callVolume = Float32Array.from(series, (value) => value ?? 0)
  const callVolumeKnown = Float32Array.from(series, (value) => (value == null ? 0 : 1))
  const zeros = new Float32Array(minutes)
  return {
    minutes,
    strikes: [7590],
    callOi: zeros,
    putOi: zeros,
    callVolume,
    putVolume: zeros,
    callVolumeKnown,
    putVolumeKnown: zeros,
    snapshotMinutes: series.map((value) => value !== undefined),
    spotSeries: series.map(() => null),
    staleAge: null,
  }
}

describe('windowVolumes (#1631)', () => {
  // Tytéž řady jako API `test_window_volumes_proti_prubeznemu_maximu` (parita)
  it.each([
    ['propad v t1 se nezapočte znovu', [1000, 800, 760, 1000, 1010], 2, 4, 10],
    ['t2 v propadu', [1000, 1005, 760], 1, 2, 5],
    ['t1 ve fallbacku bere maximum, ne 0', [100, null, 130, 150], 2, 3, 50],
    ['strike bez měření do t1 jen založí stav', [null, null, 500, 510], 2, 3, 10],
    ['okno od začátku seance nese celý kumulativ', [100, null, 130, 150], 0, 3, 150],
  ] as [string, (number | null)[], number, number, number][])(
    '%s',
    (_label, series, fromIdx, toIdx, expected) => {
      expect(windowVolumes(rawOf(series), fromIdx, toIdx)?.get(7590)).toEqual({
        call: expected,
        put: 0,
      })
    },
  )

  it('bez snapshotu před oknem = od začátku seance i s barem před ním', () => {
    // Minuta 0 má jen bar (osa začíná dřív než opční snímky)
    expect(windowVolumes(rawOf([undefined, 100, 130]), 1, 2)?.get(7590)?.call).toBe(130)
  })

  it('den bez masky objemu (Daily, demo) okno nemá', () => {
    expect(windowVolumes({ ...rawOf([100, 130]), callVolumeKnown: undefined }, 0, 1)).toBeNull()
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

describe('panelWindowCumDelta (#483, ADR-0057 bod 4)', () => {
  it('rozdíl úrovní okna, kotva před prvním měřením 0', () => {
    expect(panelWindowCumDelta([10, 25, 40], 1, 2)).toBe(30)
    expect(panelWindowCumDelta([10, 25, 40], 0, 2)).toBe(40)
    expect(panelWindowCumDelta([], 0, 1)).toBeNull()
  })
  it('mezera uvnitř okna = gap, hranice v mezeře bere poslední měřenou úroveň', () => {
    const cum = [10, null, 30, 45]
    expect(panelWindowCumDelta(cum, 1, 2)).toBe('gap')
    // okno [2, 3]: hranice t1 − 1 = minuta 1 v mezeře → úroveň 10 z minuty 0
    expect(panelWindowCumDelta(cum, 2, 3)).toBe(35)
  })
})
