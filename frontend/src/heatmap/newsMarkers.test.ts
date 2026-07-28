/** Testy markerů zpráv (#287): mapování na osu, clustering, barvy, projekce. */
import { describe, expect, test } from 'vitest'
import { buildNewsMarkers, markerAt, markerColor, markerStyle } from './newsMarkers'
import type { NewsRow } from '../api/news'

/** Zrcadlí formát osy grafu (Intl, bez vodicí nuly u hodin). */
const label = (iso: string): string => {
  const at = new Date(iso)
  return `${at.getHours()}:${String(at.getMinutes()).padStart(2, '0')}`
}

const LABELS = ['9:00', '9:01', '9:02', '9:03']

function row(overrides: Partial<NewsRow> & { id: number; hour: number; minute: number }): NewsRow {
  const { hour, minute, ...rest } = overrides
  return {
    ts_event: new Date(2026, 6, 28, hour, minute).toISOString(),
    ts_ingested: new Date(2026, 6, 28, hour, minute).toISOString(),
    source: 'rss_news',
    kind: 'headline',
    category: 'FED',
    importance: 2,
    title: `Zpráva ${rest.id}`,
    summary: null,
    sentiment_dir: null,
    sentiment_score: 0.4,
    sentiment_source: 'rule',
    forecast: null,
    previous: null,
    actual: null,
    ...rest,
  }
}

describe('buildNewsMarkers', () => {
  test('mapuje zprávy na minutu podle času', () => {
    const markers = buildNewsMarkers([row({ id: 1, hour: 9, minute: 2 })], [], LABELS, label)
    expect(markers).toHaveLength(1)
    expect(markers[0].minuteIdx).toBe(2)
    expect(markers[0].count).toBe(1)
  })

  test('víc zpráv v jedné minutě je jeden marker s počtem (SPEC 9.1)', () => {
    const markers = buildNewsMarkers(
      [
        row({ id: 1, hour: 9, minute: 1, sentiment_score: 0.5 }),
        row({ id: 2, hour: 9, minute: 1, sentiment_score: -0.2, importance: 3 }),
      ],
      [],
      LABELS,
      label,
    )
    expect(markers).toHaveLength(1)
    expect(markers[0].count).toBe(2)
    expect(markers[0].score).toBeCloseTo(0.3)
    // Cluster dědí nejvyšší důležitost, aby se silná zpráva neztratila
    expect(markers[0].importance).toBe(3)
    expect(markers[0].titles).toHaveLength(2)
  })

  test('zpráva mimo osu se zahodí, ne přilepí na okraj', () => {
    expect(buildNewsMarkers([row({ id: 1, hour: 15, minute: 0 })], [], LABELS, label)).toEqual([])
    expect(buildNewsMarkers([row({ id: 1, hour: 9, minute: 0 })], [], [], label)).toEqual([])
  })

  test('nadcházející event je dutý, ale smíchaný cluster už ne', () => {
    const future = buildNewsMarkers([], [row({ id: 9, hour: 9, minute: 3 })], LABELS, label)
    expect(future[0].upcoming).toBe(true)

    const mixed = buildNewsMarkers(
      [row({ id: 1, hour: 9, minute: 3 })],
      [row({ id: 2, hour: 9, minute: 3 })],
      LABELS,
      label,
    )
    expect(mixed[0].upcoming).toBe(false)
  })

  test('řetězcové skóre z API (PG Decimal) se nesmí ztratit', () => {
    const markers = buildNewsMarkers(
      [row({ id: 1, hour: 9, minute: 0, sentiment_score: '0.75' })],
      [],
      LABELS,
      label,
    )
    expect(markers[0].score).toBeCloseTo(0.75)
  })

  test('markery jsou seřazené podle času', () => {
    const markers = buildNewsMarkers(
      [row({ id: 1, hour: 9, minute: 3 }), row({ id: 2, hour: 9, minute: 0 })],
      [],
      LABELS,
      label,
    )
    expect(markers.map((m) => m.minuteIdx)).toEqual([0, 3])
  })
})

describe('vzhled', () => {
  const base = { minuteIdx: 0, count: 1, importance: 2, glyph: '•', titles: [] }

  test('barva odpovídá znaménku skóre', () => {
    expect(markerColor({ ...base, score: 1, upcoming: false }, 1)).toContain('20,184,166')
    expect(markerColor({ ...base, score: -1, upcoming: false }, 1)).toContain('224,82,96')
    expect(markerColor({ ...base, score: 0, upcoming: false }, 1)).toContain('125,133,150')
    // Nadcházející je vždy šedý — o jeho dopadu se zatím nic neví
    expect(markerColor({ ...base, score: 5, upcoming: true }, 1)).toContain('125,133,150')
  })

  test('důležitost řídí jas i tloušťku', () => {
    const high = markerStyle({ ...base, score: 0, upcoming: false, importance: 3 })
    const low = markerStyle({ ...base, score: 0, upcoming: false, importance: 1 })
    expect(high.alpha).toBeGreaterThan(low.alpha)
    expect(high.width).toBeGreaterThan(low.width)
  })

  test('markerAt najde marker na minutě crosshairu', () => {
    const markers = buildNewsMarkers([row({ id: 1, hour: 9, minute: 2 })], [], LABELS, label)
    expect(markerAt(markers, 2)?.count).toBe(1)
    expect(markerAt(markers, 1)).toBeNull()
    expect(markerAt(markers, null)).toBeNull()
  })
})
