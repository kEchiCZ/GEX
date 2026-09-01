/** Testy agregace timeframů: OHLC skládání, součty vs. poslední hodnoty, popisky. */
import { expect, test } from 'vitest'
import { aggregateBars, aggregateDay, aggregateLive } from './aggregate'
import { buildDailyDay, dayLabel } from './daily'
import { profileSourceOf } from './loader'
import { minuteLabel } from './useDayData'
import { buildBucketPlan } from '../heatmap/buckets'
import type { DayData } from './useDayData'
import type { ReplayDay } from './loader'
import type { PriceBar } from '../heatmap/overlays'
import type { ProfileRow } from '../profile/bars'

function sampleDay(): DayData {
  // 4 minuty × 2 strikes; vrstvy v kumulativní sémantice (roste v čase)
  const call = Float32Array.from([1, 2, 3, 4, 10, 20, 30, 40])
  const put = Float32Array.from([4, 3, 2, 1, 40, 30, 20, 10])
  const price: PriceBar[] = [
    { minuteIdx: 0, open: 100, high: 102, low: 99, close: 101, up: true },
    { minuteIdx: 1, open: 101, high: 105, low: 100, close: 104, up: true },
    { minuteIdx: 2, open: 104, high: 104, low: 95, close: 96, up: false },
    { minuteIdx: 3, open: 96, high: 98, low: 96, close: 97, up: true },
  ]
  return {
    source: 'replay',
    reconstructedIso: [],
    grid: { minutes: 4, strikes: [100, 105], layers: { call, put }, staleAge: null },
    raw: null,
    rawFa: null,
    gexProfileFa: null,
    gexFieldFa: null,
    minutesIso: [],
    overlays: {
      price,
      levels: [{ name: 'flip', color: '#fff', series: [100, null, null, 102] }],
      walls: [],
      sessions: [{ minuteIdx: 3, label: 'London' }],
      timestamp: 't',
    },
    panels: {
      vol: [10, 20, 30, 40],
      optVolCall: [1, 2, 3, 4],
      optVolPut: [4, 3, 2, 1],
      cumDelta: [5, -5, 10, 20],
      deltaFlowCall: [1, 1, 2, 2],
      deltaFlowPut: [2, 2, 1, 1],
    },
    profileByMinute: profileSourceOf([[], [], [], []]),
    demoProfileRows: null,
    spotSeries: [101, 104, 96, 97],
    minuteLabels: ['9:30', '9:31', '9:32', '9:33'],
    lastMinuteIso: '2026-07-16T15:03:00.000Z',
    gexProfile: null,
    gexField: null,
    ladder: null,
  }
}

test('aggregateBars skládá OHLC koše (open první, close poslední, high/low extrémy)', () => {
  const bars = aggregateBars(sampleDay().overlays.price!, buildBucketPlan([], 4, 2))
  expect(bars).toHaveLength(2)
  expect(bars[0]).toMatchObject({ minuteIdx: 0, open: 100, high: 105, low: 99, close: 104 })
  expect(bars[1]).toMatchObject({ minuteIdx: 1, open: 104, high: 104, low: 95, close: 97 })
  expect(bars[0].up).toBe(true) // první koš: close ≥ open
  expect(bars[1].up).toBe(false) // 97 < 104 (close předchozího koše)
})

test('aggregateLive: 1m timeframe nechá živou vrstvu beze změny (#141)', () => {
  const live = {
    bars: [{ minuteIdx: 4, open: 97, high: 99, low: 96, close: 98, up: true }],
    labels: ['9:34'],
    minutesIso: [],
  }
  expect(aggregateLive(live, 1, 4, [], [])).toBe(live) // stabilní identita
})

test('aggregateLive: rozdělaná minuta splyne s košem uzavřených minut (#141)', () => {
  // 2m koše nad 4 uzavřenými minutami → koše 0 (min 0–1) a 1 (min 2–3).
  // Rozdělaná minuta 4 patří do koše 2 (nový, prázdný).
  const statik = aggregateBars(sampleDay().overlays.price!, buildBucketPlan([], 4, 2))
  const live = {
    bars: [{ minuteIdx: 4, open: 97, high: 110, low: 90, close: 108, up: true }],
    labels: ['9:34'],
    minutesIso: [],
  }
  const merged = aggregateLive(live, 2, 4, statik, [])
  expect(merged.bars).toHaveLength(1)
  expect(merged.bars[0]).toMatchObject({ minuteIdx: 2, open: 97, high: 110, low: 90, close: 108 })
  expect(merged.labels).toEqual(['9:34']) // koš 2 je za koncem gridu (buckets = 2)
})

test('aggregateLive: živá minuta uvnitř rozpracovaného koše přebírá jeho open a extrémy (#141)', () => {
  // 4m koš nad 3 uzavřenými minutami (0–2) → koš 0. Rozdělaná minuta 3 padá do TÉHOŽ koše.
  const closed = sampleDay().overlays.price!.slice(0, 3)
  const statik = aggregateBars(closed, buildBucketPlan([], 3, 4))
  expect(statik[0]).toMatchObject({ minuteIdx: 0, open: 100, high: 105, low: 95, close: 96 })
  const live = {
    bars: [{ minuteIdx: 3, open: 96, high: 99, low: 93, close: 94, up: false }],
    labels: ['9:33'],
    minutesIso: [],
  }
  const merged = aggregateLive(live, 4, 3, statik, [])
  expect(merged.bars).toHaveLength(1)
  // open z uzavřené části koše, extrémy přes obě části, close z živého ticku
  expect(merged.bars[0]).toMatchObject({ minuteIdx: 0, open: 100, high: 105, low: 93, close: 94 })
  expect(merged.bars[0].up).toBe(false) // 94 < 100
  expect(merged.labels).toHaveLength(0) // koš 0 je uvnitř gridu, popisek už existuje
})

test('aggregateLive: barva koše se řídí close předchozího koše, ne vlastním open (#159)', () => {
  const statik = aggregateBars(sampleDay().overlays.price!, buildBucketPlan([], 4, 2)) // koš 1 zavírá na 97
  const live = {
    bars: [{ minuteIdx: 4, open: 100, high: 101, low: 94, close: 98, up: false }],
    labels: ['9:34'],
    minutesIso: [],
  }
  const merged = aggregateLive(live, 2, 4, statik, [])
  // close 98 < open 100, ale >= close předchozího koše (97) → zelená,
  // stejně jako ji po uzavření obarví aggregateBars
  expect(merged.bars[0].up).toBe(true)
})

test('aggregateDay: kumulativní vrstvy berou poslední minutu koše, Vol se sčítá', () => {
  const day = aggregateDay(sampleDay(), 2)
  expect(day.grid.minutes).toBe(2)
  // Vrstva call, strike 100: koš 0 = minuta 1 (hodnota 2), koš 1 = minuta 3 (hodnota 4)
  expect(Array.from(day.grid.layers.call!.slice(0, 2))).toEqual([2, 4])
  expect(Array.from(day.grid.layers.call!.slice(2, 4))).toEqual([20, 40])
  expect(day.panels.vol).toEqual([30, 70]) // součet přírůstků
  expect(day.panels.cumDelta).toEqual([-5, 20]) // poslední hodnota koše
  expect(day.spotSeries).toEqual([104, 97])
  expect(day.minuteLabels).toEqual(['9:30', '9:32']) // začátek koše
  expect(day.overlays.levels![0].series).toEqual([100, 102]) // poslední ne-null
  expect(day.overlays.sessions![0].minuteIdx).toBe(1)
})

test('aggregateDay: koš končící v díře přebírá profil poslední minuty se snapshotem (#503)', () => {
  const measuredRow: ProfileRow = {
    strike: 100,
    callVolComponent: 1,
    callOiComponent: 2,
    putVolComponent: 3,
    putOiComponent: 4,
    callVolume: 1,
    putVolume: 3,
    callOi: 2,
    putOi: 4,
    distanceFromSpot: 0,
  }
  const day = sampleDay()
  // Minuta 3 je díra (bez snapshotu → prázdný profil), minuty 0–2 měřené
  day.profileByMinute = profileSourceOf([[measuredRow], [measuredRow], [measuredRow], []])
  const coarse = aggregateDay(day, 2)
  // Koš 1 (minuty 2–3) končí v díře → profil poslední měřené minuty (2), ne prázdno
  expect(coarse.profileByMinute!.rowsAt(1)).toEqual([measuredRow])
  expect(coarse.profileByMinute!.rowsAt(0)).toEqual([measuredRow])
  // Koš bez jediné měřené minuty zůstává prázdný
  const empty = sampleDay()
  empty.profileByMinute = profileSourceOf([[measuredRow], [measuredRow], [], []])
  expect(aggregateDay(empty, 2).profileByMinute!.rowsAt(1)).toEqual([])
})

test('aggregateDay: bucketMinutes 1 vrací originál, neúplný poslední koš se ořeže', () => {
  const original = sampleDay()
  expect(aggregateDay(original, 1)).toBe(original)
  const coarse = aggregateDay(original, 3) // 4 minuty → koše [0..2], [3]
  expect(coarse.grid.minutes).toBe(2)
  expect(coarse.panels.vol).toEqual([60, 40])
})

/** Den, jehož osa začíná MIMO hranici koše (23:59Z) — reprodukce #584.
Osa: 23:59, 00:00, 00:01, 00:02, 00:03, 00:04 (6 minut, 2 strikes). */
function offsetAxisDay(): DayData {
  const minutesIso = [
    '2026-08-09T23:59:00.000Z',
    '2026-08-10T00:00:00.000Z',
    '2026-08-10T00:01:00.000Z',
    '2026-08-10T00:02:00.000Z',
    '2026-08-10T00:03:00.000Z',
    '2026-08-10T00:04:00.000Z',
  ]
  const day = sampleDay()
  const minutes = minutesIso.length
  return {
    ...day,
    minutesIso,
    minuteLabels: minutesIso.map(minuteLabel),
    grid: {
      minutes,
      strikes: [100, 105],
      layers: {
        call: Float32Array.from([1, 2, 3, 4, 5, 6, 10, 20, 30, 40, 50, 60]),
        put: Float32Array.from([6, 5, 4, 3, 2, 1, 60, 50, 40, 30, 20, 10]),
      },
      staleAge: null,
    },
    overlays: {
      price: [
        { minuteIdx: 0, open: 100, high: 101, low: 99, close: 100, up: true },
        { minuteIdx: 1, open: 100, high: 106, low: 100, close: 105, up: true },
        { minuteIdx: 2, open: 105, high: 105, low: 104, close: 104, up: false },
        { minuteIdx: 3, open: 104, high: 104, low: 103, close: 103, up: false },
        { minuteIdx: 4, open: 103, high: 108, low: 103, close: 107, up: true },
        { minuteIdx: 5, open: 107, high: 107, low: 102, close: 102, up: false },
      ],
      levels: [],
      walls: [],
      sessions: [{ minuteIdx: 1, label: 'Tokyo' }],
      timestamp: 't',
    },
    panels: {
      vol: [1, 2, 4, 8, 16, 32],
      optVolCall: [0, 0, 0, 0, 0, 0],
      optVolPut: [0, 0, 0, 0, 0, 0],
      cumDelta: [1, 2, 3, 4, 5, 6],
      deltaFlowCall: [0, 0, 0, 0, 0, 0],
      deltaFlowPut: [0, 0, 0, 0, 0, 0],
    },
    profileByMinute: profileSourceOf([[], [], [], [], [], []]),
    spotSeries: [100, 105, 104, 103, 107, 102],
    lastMinuteIso: minutesIso[minutes - 1],
  }
}

test('aggregateDay: koše jsou zarovnané na wall-clock, ne na začátek osy (#584)', () => {
  const day = aggregateDay(offsetAxisDay(), 5)
  // 23:59 je vlastní (neúplný) koš, 00:00–00:04 je druhý — ne 23:59–00:03 + zbytek
  expect(day.grid.minutes).toBe(2)
  expect(day.minuteLabels).toEqual([
    minuteLabel('2026-08-09T23:55:00.000Z'), // hranice koše, ne první minuta v něm
    minuteLabel('2026-08-10T00:00:00.000Z'),
  ])
  // Vol: koš 23:55 = jen minuta 23:59, koš 00:00 = zbytek
  expect(day.panels.vol).toEqual([1, 62])
  expect(day.panels.cumDelta).toEqual([1, 6]) // poslední minuta koše
  expect(day.spotSeries).toEqual([100, 102])
  // Kumulativní vrstva: poslední minuta koše (strike 100 → 1 a 6)
  expect(Array.from(day.grid.layers.call!.slice(0, 2))).toEqual([1, 6])
  // Svíčka koše 00:00 otevírá na 00:00 a zavírá na 00:04
  expect(day.overlays.price![1]).toMatchObject({ open: 100, high: 108, low: 100, close: 102 })
  expect(day.overlays.sessions![0].minuteIdx).toBe(1) // marker minuty 00:00 → druhý koš
})

test('aggregateDay: 15m koš nad osou od 23:59 taky drží hranici (#584)', () => {
  const day = aggregateDay(offsetAxisDay(), 15)
  expect(day.grid.minutes).toBe(2) // 23:45–23:59 a 00:00–00:14
  expect(day.minuteLabels).toEqual([
    minuteLabel('2026-08-09T23:45:00.000Z'),
    minuteLabel('2026-08-10T00:00:00.000Z'),
  ])
})

test('aggregateLive: koš náběžné hrany je wall-clock hranice (#584)', () => {
  const day = offsetAxisDay()
  const coarse = aggregateDay(day, 5)
  // Rozdělaná minuta 00:05 (index 6 = za koncem osy) otevírá NOVÝ koš 00:05
  const live = {
    bars: [{ minuteIdx: 6, open: 102, high: 104, low: 101, close: 103, up: true }],
    labels: [minuteLabel('2026-08-10T00:05:00.000Z')],
    minutesIso: ['2026-08-10T00:05:00.000Z'],
  }
  const merged = aggregateLive(live, 5, day.grid.minutes, coarse.overlays.price ?? [], day.minutesIso) // prettier-ignore
  expect(merged.bars).toHaveLength(1)
  expect(merged.bars[0].minuteIdx).toBe(2) // koš za dvěma naměřenými
  expect(merged.labels).toEqual([minuteLabel('2026-08-10T00:05:00.000Z')])

  // Minuta 00:04 (poslední naměřená) by naopak splynula s košem 00:00 —
  // živá minuta uvnitř rozpracovaného koše nesmí založit nový sloupec
  const inside = {
    bars: [{ minuteIdx: 5, open: 103, high: 109, low: 103, close: 109, up: true }],
    labels: [],
    minutesIso: [],
  }
  const sameBucket = aggregateLive(inside, 5, day.grid.minutes, coarse.overlays.price ?? [], day.minutesIso) // prettier-ignore
  expect(sameBucket.bars).toHaveLength(1)
  expect(sameBucket.bars[0]).toMatchObject({ minuteIdx: 1, open: 100, high: 109, close: 109 })
  expect(sameBucket.labels).toHaveLength(0)
})

test('buildDailyDay: sloupec = den, denní OHLC svíčka a součty', () => {
  const dayA: ReplayDay = {
    symbol: 'ES',
    reconstructedIso: [],
    expiry: '20260715',
    date: '2026-07-15',
    minutes: ['a', 'b'],
    raw: {
      minutes: 2,
      strikes: [100],
      callOi: Float32Array.from([1, 1]),
      putOi: Float32Array.from([1, 1]),
      callVolume: Float32Array.from([0, 0]),
      putVolume: Float32Array.from([0, 0]),
      spotSeries: [102, 105],
      staleAge: null,
    },
    grid: {
      minutes: 2,
      strikes: [100],
      layers: { call: Float32Array.from([0.5, 0.8]), put: Float32Array.from([0.2, 0.4]) },
      staleAge: null,
    },
    overlays: {
      price: [
        { minuteIdx: 0, open: 100, high: 103, low: 99, close: 102, up: true },
        { minuteIdx: 1, open: 102, high: 106, low: 101, close: 105, up: true },
      ],
      levels: [{ name: 'flip', color: '#fff', series: [101, 102] }],
      walls: [],
    },
    panels: {
      vol: [10, 20],
      optVolCall: [1, 2],
      optVolPut: [3, 4],
      cumDelta: [5, 15],
      deltaFlowCall: [1, 2],
      deltaFlowPut: [2, 1],
    },
    profileByMinute: profileSourceOf([[], []]),
    provisionalMinutes: [],
    gexProfile: [null, null],
    gexField: null,
    rawFa: null,
    gexProfileFa: null,
    gexFieldFa: null,
    ladder: [],
  }
  const dayB: ReplayDay = {
    ...dayA,
    date: '2026-07-16',
    expiry: '20260716',
    overlays: {
      price: [{ minuteIdx: 0, open: 105, high: 107, low: 100, close: 101, up: false }],
      levels: [{ name: 'flip', color: '#fff', series: [103, null] }],
      walls: [],
    },
  }

  const daily = buildDailyDay([dayA, dayB])
  expect(daily.grid.minutes).toBe(2) // 2 dny = 2 sloupce
  // Poslední minuta dne (Float32 přesnost → closeTo)
  for (const value of daily.grid.layers.call!) {
    expect(value).toBeCloseTo(0.8, 5)
  }
  expect(daily.panels.vol).toEqual([30, 30])
  expect(daily.panels.cumDelta).toEqual([15, 15])
  expect(daily.overlays.price![0]).toMatchObject({ open: 100, high: 106, low: 99, close: 105 })
  expect(daily.overlays.price![1].up).toBe(false) // 101 < 105
  expect(daily.overlays.levels![0].series).toEqual([102, 103])
  expect(daily.minuteLabels).toEqual(['15.7.', '16.7.'])
})

test('dayLabel formátuje ISO datum česky', () => {
  expect(dayLabel('2026-07-16')).toBe('16.7.')
  expect(dayLabel('nesmysl')).toBe('nesmysl')
})

function makeReplayDay(date: string): ReplayDay {
  return {
    symbol: 'ES',
    reconstructedIso: [],
    expiry: date.replaceAll('-', ''),
    date,
    minutes: ['a'],
    raw: {
      minutes: 1,
      strikes: [100],
      callOi: Float32Array.from([1]),
      putOi: Float32Array.from([1]),
      callVolume: Float32Array.from([0]),
      putVolume: Float32Array.from([0]),
      spotSeries: [102],
      staleAge: null,
    },
    grid: {
      minutes: 1,
      strikes: [100],
      layers: { call: Float32Array.from([0.5]), put: Float32Array.from([0.2]) },
      staleAge: null,
    },
    overlays: {
      price: [{ minuteIdx: 0, open: 100, high: 103, low: 99, close: 102, up: true }],
      levels: [],
      walls: [],
    },
    panels: {
      vol: [10],
      optVolCall: [1],
      optVolPut: [3],
      cumDelta: [5],
      deltaFlowCall: [1],
      deltaFlowPut: [2],
    },
    profileByMinute: profileSourceOf([[]]),
    provisionalMinutes: [],
    gexProfile: [null],
    gexField: null,
    rawFa: null,
    gexProfileFa: null,
    gexFieldFa: null,
    ladder: [],
  }
}

test('buildDailyDay: chybějící den = šrafovaný sloupec, ne tiché vynechání (#516)', () => {
  const dayA = makeReplayDay('2026-07-16')
  const dayB = makeReplayDay('2026-07-18')
  const daily = buildDailyDay([dayA, dayB], ['2026-07-17'])
  // Tři sloupce chronologicky — díra uprostřed zůstává v ose
  expect(daily.grid.minutes).toBe(3)
  expect(daily.minuteLabels).toEqual([dayLabel('2026-07-16'), dayLabel('2026-07-17'), dayLabel('2026-07-18')]) // prettier-ignore
  expect(daily.grid.missingMinutes).toEqual([false, true, false])
  // Chybějící sloupec je prázdný (nula ≠ data, vizuál nese šrafura + tooltip)
  const strikes = daily.grid.strikes.length
  for (let strikeIdx = 0; strikeIdx < strikes; strikeIdx += 1) {
    expect(daily.grid.layers.call?.[strikeIdx * 3 + 1] ?? 0).toBe(0)
  }
  // Bez děr příznak není (žádná režie pro běžný den)
  expect(buildDailyDay([dayA, dayB]).grid.missingMinutes ?? null).toBeNull()
})
