/** Testy markerů zpráv (#287, #1290): časové mapování na koše, clustering,
barvy, projekce, historie a plán kreslení. */
import { describe, expect, test } from 'vitest'
import {
  NEWS_DETAIL_MIN_PX,
  buildNewsMarkers,
  closedDayMarkers,
  expectedImpact,
  markerAt,
  markerColor,
  markerFromRows,
  markerStyle,
  newsBucketIndex,
  newsDrawPlan,
  newsMarkerAtX,
  significantOnly,
} from './newsMarkers'
import type { AxisSegment, NewsMarker, TimedNewsRow } from './newsMarkers'
import { bucketStartsMs } from './buckets'
import { sessionBoundsUtc } from '../instrument/tz'

/** Seance 16. 9. 2026 (CDT): open 15. 9. 22:00Z, pauza CME 21:00–22:00Z. */
const SESSION = '2026-09-16'
const { openMs: OPEN, closeMs: CLOSE } = sessionBoundsUtc(SESSION)
const MINUTE = 60_000
/** Obchodní minuty seance: 23 h od otevření, poslední hodina (pauza) bez sloupce. */
const TRADING_MINUTES = 23 * 60

function isoAt(ms: number): string {
  return new Date(ms).toISOString()
}

function sessionMinutes(count = TRADING_MINUTES, from = OPEN): string[] {
  return Array.from({ length: count }, (_, index) => isoAt(from + index * MINUTE))
}

/** Úsek osy zobrazeného dne pro TF `bucketMinutes` (bez projekce). */
function daySegment(minutesIso: string[], bucketMinutes = 1): AxisSegment {
  const startsMs = bucketStartsMs(minutesIso, minutesIso.length, bucketMinutes)
  if (!startsMs) throw new Error('osa bez časů')
  return { openMs: OPEN, closeMs: CLOSE, startsMs, bucketMs: bucketMinutes * MINUTE, firstIdx: 0 }
}

function row(overrides: Partial<TimedNewsRow> & { id: number; at: number }): TimedNewsRow {
  const { at, ...rest } = overrides
  return {
    ts_event: isoAt(at),
    tsMs: at,
    kind: 'headline',
    category: 'FED',
    importance: 2,
    title: `Zpráva ${rest.id}`,
    summary: null,
    sentiment_dir: null,
    sentiment_score: 0.4,
    forecast: null,
    previous: null,
    actual: null,
    ...rest,
  }
}

/** Minuta seance → epoch ms (0 = otevření). */
const minute = (index: number, seconds = 0): number => OPEN + index * MINUTE + seconds * 1000
/** Po všem — nic v testu není nadcházející, pokud to test neřekne. */
const LATER = CLOSE + 7 * 24 * 3_600_000

describe('buildNewsMarkers — časové mapování (#1290)', () => {
  const segment = daySegment(sessionMinutes())

  test('mapuje zprávu na minutu podle času, sekundy se ignorují', () => {
    const markers = buildNewsMarkers([row({ id: 1, at: minute(2, 40) })], [segment], LATER)
    expect(markers).toHaveLength(1)
    expect(markers[0].minuteIdx).toBe(2)
    expect(markers[0].count).toBe(1)
  })

  test('> 100 zpráv za den: marker dostanou všechny (dřív jen posledních 100)', () => {
    // 1 500 zpráv rovnoměrně přes seanci včetně pauzy CME — žádná nezmizí
    const rows = Array.from({ length: 1500 }, (_, index) =>
      row({ id: index + 1, at: OPEN + Math.floor((index * (CLOSE - OPEN)) / 1500) }),
    )
    const markers = buildNewsMarkers(rows, [segment], LATER)
    expect(markers.reduce((sum, marker) => sum + marker.count, 0)).toBe(1500)
    expect(markers.every((marker) => marker.minuteIdx >= 0)).toBe(true)
    expect(markers.every((marker) => marker.minuteIdx < TRADING_MINUTES)).toBe(true)
  })

  test('na 5m padne 13:02 do koše 13:00 (dřív shoda popisku → zmizela)', () => {
    const five = daySegment(sessionMinutes(), 5)
    // 13:02 CDT = 18:02Z = minuta 1202 od otevření (22:00Z předchozího dne)
    const at = Date.UTC(2026, 8, 16, 18, 2)
    const markers = buildNewsMarkers([row({ id: 1, at })], [five], LATER)
    expect(markers).toHaveLength(1)
    expect(five.startsMs[markers[0].minuteIdx]).toBe(Date.UTC(2026, 8, 16, 18, 0))
  })

  test('zpráva z pauzy CME se přimkne k poslednímu koši před pauzou', () => {
    // 16:30 CT = 21:30Z — osa tu sloupec nemá
    const markers = buildNewsMarkers(
      [row({ id: 1, at: Date.UTC(2026, 8, 16, 21, 30) })],
      [segment],
      LATER,
    )
    expect(markers[0].minuteIdx).toBe(TRADING_MINUTES - 1)
  })

  test('díra ve sběru i začátek osy po otevření: zpráva se neztratí', () => {
    // Osa začíná až 30 min po otevření (pozdní start enginu)
    const late = daySegment(sessionMinutes(60, OPEN + 30 * MINUTE))
    const markers = buildNewsMarkers(
      [row({ id: 1, at: minute(5) }), row({ id: 2, at: minute(45) })],
      [late],
      LATER,
    )
    expect(markers.map((marker) => marker.minuteIdx)).toEqual([0, 15])
  })

  test('zpráva jiné seance se nekreslí (patří jinému dni)', () => {
    const markers = buildNewsMarkers(
      [row({ id: 1, at: OPEN - MINUTE }), row({ id: 2, at: CLOSE })],
      [segment],
      LATER,
    )
    expect(markers).toEqual([])
    expect(buildNewsMarkers([row({ id: 1, at: minute(1) })], [], LATER)).toEqual([])
  })

  test('víc zpráv v jednom koši je jeden marker s počtem (SPEC 9.1)', () => {
    const markers = buildNewsMarkers(
      [
        row({ id: 1, at: minute(1), sentiment_score: 0.5 }),
        row({ id: 2, at: minute(1, 30), sentiment_score: -0.2, importance: 3 }),
      ],
      [segment],
      LATER,
    )
    expect(markers).toHaveLength(1)
    expect(markers[0].count).toBe(2)
    expect(markers[0].score).toBeCloseTo(0.3)
    // Cluster dědí nejvyšší důležitost, aby se silná zpráva neztratila
    expect(markers[0].importance).toBe(3)
    // Cluster nese celé zprávy pro dialog (#408)
    expect(markers[0].rows.map((item) => item.id)).toEqual([1, 2])
  })

  test('řetězcové skóre z API (PG Decimal) se nesmí ztratit', () => {
    const markers = buildNewsMarkers(
      [row({ id: 1, at: minute(0), sentiment_score: '0.75' })],
      [segment],
      LATER,
    )
    expect(markers[0].score).toBeCloseTo(0.75)
  })

  test('markery jsou seřazené podle času', () => {
    const markers = buildNewsMarkers(
      [row({ id: 1, at: minute(3) }), row({ id: 2, at: minute(0) })],
      [segment],
      LATER,
    )
    expect(markers.map((marker) => marker.minuteIdx)).toEqual([0, 3])
  })
})

describe('nadcházející eventy a projekce', () => {
  // Živý den do 10:00 (minuta 600) + 60 minut projekce
  const liveMinutes = sessionMinutes(600)
  const starts = bucketStartsMs(liveMinutes, liveMinutes.length, 1)!
  const withProjection = Float64Array.from([
    ...starts,
    ...Array.from({ length: 60 }, (_, index) => starts[starts.length - 1] + (index + 1) * MINUTE),
  ])
  const live: AxisSegment = {
    openMs: OPEN,
    closeMs: Number.POSITIVE_INFINITY,
    startsMs: withProjection,
    bucketMs: MINUTE,
    firstIdx: 0,
  }
  const now = minute(600)

  test('nadcházející event padne do projekce a je dutý', () => {
    const markers = buildNewsMarkers(
      [row({ id: 9, at: minute(630), kind: 'scheduled' })],
      [live],
      now,
    )
    expect(markers[0].minuteIdx).toBe(630)
    expect(markers[0].upcoming).toBe(true)
  })

  test('nadcházející za horizontem projekce se nekreslí (nepřilepí se na hranu)', () => {
    const cpi = row({ id: 9, at: minute(700), kind: 'scheduled' })
    expect(buildNewsMarkers([cpi], [live], now)).toEqual([])
  })

  test('titulek po posledním tiku „teď" není nadcházející — plný marker na hraně', () => {
    // WS titulek 15 s po posledním tiku: dřív dutý marker v projekci s odpočtem
    const markers = buildNewsMarkers([row({ id: 5, at: minute(600, 15) })], [live], now)
    expect(markers[0].upcoming).toBe(false)
    expect(markers[0].minuteIdx).toBe(600)
    // Bez projekce (minuta ještě mimo grid) se přimkne k živé hraně, nezmizí
    const noProjection: AxisSegment = { ...live, startsMs: starts }
    const edge = buildNewsMarkers([row({ id: 5, at: minute(600, 15) })], [noProjection], now)
    expect(edge[0].minuteIdx).toBe(599)
    expect(edge[0].upcoming).toBe(false)
  })

  test('budoucí scheduled ze dne i z /news/upcoming = jeden dutý marker', () => {
    // Dřív: count 2 a plný marker (tatáž zpráva ve `news` i `upcoming`)
    const cpi = row({ id: 42, at: minute(630), kind: 'scheduled' })
    const markers = buildNewsMarkers([cpi, { ...cpi }], [live], now)
    expect(markers).toHaveLength(1)
    expect(markers[0].count).toBe(1)
    expect(markers[0].upcoming).toBe(true)
  })

  test('smíchaný cluster (proběhlé + nadcházející) už dutý není', () => {
    const markers = buildNewsMarkers(
      [
        row({ id: 1, at: minute(600), kind: 'scheduled' }),
        row({ id: 2, at: minute(600, 30), kind: 'scheduled' }),
      ],
      [live],
      minute(600, 10),
    )
    expect(markers[0].upcoming).toBe(false)
  })

  test('proběhlá zpráva za koncem gridu (minuta ještě nedorazila) se přimkne k hraně', () => {
    const noProjection: AxisSegment = { ...live, startsMs: starts }
    const markers = buildNewsMarkers(
      [row({ id: 1, at: minute(601) })],
      [noProjection],
      now + MINUTE,
    )
    expect(markers[0].minuteIdx).toBe(599)
  })
})

describe('historie (#788): záporné koše', () => {
  test('zpráva historické seance dostane index slice (záporný)', () => {
    const today = daySegment(sessionMinutes())
    const previousBounds = sessionBoundsUtc('2026-09-15')
    const previousMinutes = sessionMinutes(TRADING_MINUTES, previousBounds.openMs)
    const history: AxisSegment = {
      ...previousBounds,
      startsMs: bucketStartsMs(previousMinutes, previousMinutes.length, 1)!,
      bucketMs: MINUTE,
      firstIdx: -TRADING_MINUTES,
    }
    const markers = buildNewsMarkers(
      [row({ id: 1, at: previousBounds.openMs + 10 * MINUTE }), row({ id: 2, at: minute(10) })],
      [today, history],
      LATER,
    )
    expect(markers.map((marker) => marker.minuteIdx)).toEqual([-TRADING_MINUTES + 10, 10])
  })

  test('uzavřený den: markery z cache per identita dne, osy a filtru', () => {
    const today = daySegment(sessionMinutes())
    const day = new Map([
      [1, row({ id: 1, at: minute(3), importance: 1 })],
      [2, row({ id: 2, at: minute(7), importance: 3 })],
    ])
    const none = new Set<number>()
    const first = closedDayMarkers(day, today, false, none)
    expect(first.map((marker) => marker.minuteIdx)).toEqual([3, 7])
    // Tentýž den, osa i filtr → tatáž pole (žádná přestavba při WS pushi)
    expect(closedDayMarkers(day, today, false, none)).toBe(first)
    // Filtr „Významné" je jiný klíč; připnuté id upozornění projde i jím
    expect(closedDayMarkers(day, today, true, none).map((marker) => marker.minuteIdx)).toEqual([7])
    const pinned = closedDayMarkers(day, today, true, new Set([1]))
    expect(pinned.map((marker) => marker.minuteIdx)).toEqual([3, 7])
    // Nová identita dne (nové načtení) přestaví
    expect(closedDayMarkers(new Map(day), today, false, none)).not.toBe(first)
    // Uzavřený den nemá nic nadcházejícího, ani plánované
    const scheduled = new Map([[3, row({ id: 3, at: minute(9), kind: 'scheduled' })]])
    expect(closedDayMarkers(scheduled, today, false, none)[0].upcoming).toBe(false)
  })
})

describe('newsBucketIndex a markerFromRows (proklik z upozornění)', () => {
  test('index koše pro čas upozornění', () => {
    const five = daySegment(sessionMinutes(), 5)
    expect(newsBucketIndex(minute(62), false, [five])).toBe(12)
    expect(newsBucketIndex(OPEN - MINUTE, false, [five])).toBeNull()
  })

  test('marker ze zpráv upozornění skládá cluster jako graf', () => {
    const marker = markerFromRows(
      [
        { ...row({ id: 1, at: minute(1) }), sentiment_score: -0.5, importance: 3 },
        row({ id: 2, at: minute(1) }),
      ],
      -1,
      LATER,
    )
    expect(marker?.count).toBe(2)
    expect(marker?.importance).toBe(3)
    expect(marker?.upcoming).toBe(false)
    expect(markerFromRows([], -1, LATER)).toBeNull()
    // Titulek s časem po „teď" nadcházející není, plánovaný event ano
    expect(markerFromRows([row({ id: 3, at: minute(5) })], -1, minute(1))?.upcoming).toBe(false)
    const scheduled = row({ id: 4, at: minute(5), kind: 'scheduled' })
    expect(markerFromRows([scheduled], -1, minute(1))?.upcoming).toBe(true)
  })
})

describe('vzhled', () => {
  const base: NewsMarker = {
    minuteIdx: 0,
    count: 1,
    score: 0,
    importance: 2,
    glyph: '•',
    upcoming: false,
    rows: [],
  }

  test('barva odpovídá znaménku skóre', () => {
    expect(markerColor({ ...base, score: 1 }, 1)).toContain('20,184,166')
    expect(markerColor({ ...base, score: -1 }, 1)).toContain('224,82,96')
    expect(markerColor({ ...base, score: 0 }, 1)).toContain('125,133,150')
    // Nadcházející je vždy šedý — o jeho dopadu se zatím nic neví
    expect(markerColor({ ...base, score: 5, upcoming: true }, 1)).toContain('125,133,150')
  })

  test('důležitost řídí jas i tloušťku', () => {
    const high = markerStyle({ ...base, importance: 3 })
    const low = markerStyle({ ...base, importance: 1 })
    expect(high.alpha).toBeGreaterThan(low.alpha)
    expect(high.width).toBeGreaterThan(low.width)
  })

  test('markerAt najde marker na minutě crosshairu', () => {
    const markers = buildNewsMarkers(
      [row({ id: 1, at: minute(2) })],
      [daySegment(sessionMinutes())],
      LATER,
    )
    expect(markerAt(markers, 2)?.count).toBe(1)
    expect(markerAt(markers, 1)).toBeNull()
    expect(markerAt(markers, null)).toBeNull()
  })
})

describe('plán kreslení (#1290): viewport a hrubý zoom', () => {
  const marker = (minuteIdx: number, importance = 1, count = 1): NewsMarker => ({
    minuteIdx,
    count,
    score: 0.4,
    importance,
    glyph: '🏛',
    upcoming: false,
    rows: [],
  })
  const toX =
    (scaleX: number, offsetX = 0) =>
    (index: number) =>
      (index + 0.5) * scaleX + offsetX

  test('markery mimo viewport se nekreslí', () => {
    const markers = [marker(-100), marker(10), marker(50), marker(500)]
    // 10 px na koš, plátno 1 000 px, pohled posunutý o −200 px → viditelné koše 20–119
    const plan = newsDrawPlan(markers, toX(10, -200), 10, 1000)
    const ticks = [...plan.ticks.values()].flat()
    expect(ticks.map((tick) => tick.x)).toEqual([300])
    expect(plan.glyphs).toHaveLength(1)
  })

  test('detailní zoom: glyf a počet u každého markeru', () => {
    const plan = newsDrawPlan([marker(1, 1, 3), marker(5, 2)], toX(20), 20, 1000)
    const byX = [...plan.glyphs].sort((a, b) => a.x - b.x)
    expect(byX.map((glyph) => glyph.count)).toEqual([3, null])
  })

  test(`pod ${NEWS_DETAIL_MIN_PX} px na koš: čárky všude, glyf jen významné a bez počtu`, () => {
    const markers = [marker(10, 1, 5), marker(40, 2, 4), marker(80, 3)]
    const plan = newsDrawPlan(markers, toX(1), 1, 1000)
    expect([...plan.ticks.values()].flat()).toHaveLength(3)
    expect(plan.glyphs).toHaveLength(2)
    expect(plan.glyphs.every((glyph) => glyph.count === null)).toBe(true)
  })

  test('glyfy se neslévají, přednost má vyšší důležitost; čárka zůstává', () => {
    // Dva markery 2 px od sebe: kreslí se glyf důležitějšího
    const plan = newsDrawPlan([marker(100, 2), marker(102, 3)], toX(1), 1, 1000)
    expect(plan.glyphs).toHaveLength(1)
    expect(plan.glyphs[0].x).toBeCloseTo(102)
    expect([...plan.ticks.values()].flat()).toHaveLength(2)
  })

  test('čárky se seskupí podle stylu — jeden stroke na skupinu', () => {
    const markers = Array.from({ length: 300 }, (_, index) => marker(index * 3, 1 + (index % 3)))
    const plan = newsDrawPlan(markers, toX(3), 3, 1000)
    // Tři důležitosti × jedna barva = tři skupiny, ne 300 strokeů
    expect(plan.ticks.size).toBe(3)
  })
})

describe('interakce a filtr (#408)', () => {
  const plain = (minuteIdx: number, importance = 1): NewsMarker => ({
    minuteIdx,
    count: 1,
    score: 0.4,
    importance,
    glyph: '🏛',
    upcoming: false,
    rows: [],
  })
  const toX = (scaleX: number) => (index: number) => (index + 0.5) * scaleX

  test('newsMarkerAtX: detailní zoom — klik na glyf i vedle čárky trefí nejbližší', () => {
    // 20 px na koš: čárka koše k je v x = 20k, glyf má střed x + 2
    const markers = [plain(0), plain(3)]
    expect(newsMarkerAtX(markers, toX(20), 20, 1000, 3)?.minuteIdx).toBe(0)
    expect(newsMarkerAtX(markers, toX(20), 20, 1000, 62)?.minuteIdx).toBe(3)
    // Mimo dosah nic — klik doprostřed prázdné plochy dialog neotvírá
    expect(newsMarkerAtX(markers, toX(20), 20, 1000, 30)).toBeNull()
  })

  test('newsMarkerAtX: hustá osa — klik na viditelný glyf otevře jeho zprávu, ne soused', () => {
    // 1,2 px na koš (celý den na 1m): drobné zprávy každou minutu bez glyfu,
    // významná na koši 500 s glyfem v x = 600
    const markers = Array.from({ length: 1100 }, (_, index) => plain(index, index === 500 ? 3 : 1))
    const plan = newsDrawPlan(markers, toX(1.2), 1.2, 1400)
    expect(plan.glyphs.map((glyph) => glyph.marker.minuteIdx)).toEqual([500])
    // Klik na střed glyfu (x + 2): nejbližší koš podle indexu by byl 501 (drobná)
    expect(newsMarkerAtX(markers, toX(1.2), 1.2, 1400, 602)?.minuteIdx).toBe(500)
    // Mimo glyfy platí nejbližší čárka
    expect(newsMarkerAtX(markers, toX(1.2), 1.2, 1400, 120.1)?.minuteIdx).toBe(100)
  })

  test('expectedImpact: klasifikovaný směr má přednost, jinak znaménko skóre', () => {
    expect(expectedImpact(row({ id: 1, at: 0, sentiment_dir: -1, sentiment_score: 0.9 }))).toBe(-1)
    expect(expectedImpact(row({ id: 2, at: 0, sentiment_score: 0.4 }))).toBe(1)
    expect(expectedImpact(row({ id: 3, at: 0, sentiment_score: '-0.2' }))).toBe(-1)
    expect(expectedImpact(row({ id: 4, at: 0, sentiment_score: null }))).toBe(0)
  })

  test('significantOnly: pouští importance ≥ 2, chybějící důležitost je okrajová', () => {
    const rows = [
      row({ id: 1, at: 0, importance: 1 }),
      row({ id: 2, at: 0, importance: 2 }),
      row({ id: 3, at: 0, importance: 3 }),
      row({ id: 4, at: 0, importance: null }),
    ]
    expect(significantOnly(rows).map((item) => item.id)).toEqual([2, 3])
    // Zprávy prokliknutého upozornění projdou vždy (makro podle FF impactu, #1290)
    expect(significantOnly(rows, new Set([1])).map((item) => item.id)).toEqual([1, 2, 3])
  })
})

describe('výkon (#1274, #1290)', () => {
  test('špičková seance (~5 000 zpráv) se složí do markerů a plánu rychle', () => {
    // 16. 9. 2026: 4 971 zpráv; 1 240 minut se zprávou
    const rows = Array.from(
      { length: 5000 },
      (_, index) =>
      row({ id: index + 1, at: minute(Math.floor((index * TRADING_MINUTES) / 5000)), importance: 1 + (index % 3) }), // prettier-ignore
    )
    const segment = daySegment(sessionMinutes())
    const started = performance.now()
    const markers = buildNewsMarkers(rows, [segment], LATER)
    const plan = newsDrawPlan(markers, (index) => (index + 0.5) * 1.2, 1.2, 1650)
    const elapsed = performance.now() - started
    expect(markers.reduce((sum, marker) => sum + marker.count, 0)).toBe(5000)
    expect(plan.glyphs.length).toBeLessThanOrEqual(Math.ceil(1650 / 8) + 1)
    // Rozpočet s rezervou pro přehřátý notebook; typicky jednotky ms
    expect(elapsed).toBeLessThan(250)
  })
})
