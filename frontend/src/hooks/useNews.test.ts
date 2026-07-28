/** Testy živého feedu zpráv (#335): slučování WS pushů do seznamu. */
import { describe, expect, test } from 'vitest'
import type { NewsRow } from '../api/news'
import { mergeNewsRow } from './useNews'

function row(id: number, ts: string, category: string | null = null): NewsRow {
  return {
    id,
    ts_event: ts,
    ts_ingested: ts,
    source: 'ibkr_brfg',
    kind: 'broker',
    category,
    importance: null,
    title: `zpráva ${id}`,
    summary: null,
    sentiment_dir: null,
    sentiment_score: null,
    sentiment_source: null,
    forecast: null,
    previous: null,
    actual: null,
  }
}

describe('mergeNewsRow', () => {
  test('klasifikovaná verze nahradí syrovou, ne přidá druhý řádek', () => {
    // Engine pushne titulek hned, news-engine tentýž event po klasifikaci
    const raw = row(7, '2026-07-28T12:00:00Z')
    const classified = row(7, '2026-07-28T12:00:00Z', 'FED')

    const feed = mergeNewsRow(mergeNewsRow([], raw), classified)

    expect(feed).toHaveLength(1)
    expect(feed[0].category).toBe('FED')
  })

  test('řadí podle času vzniku, ne podle pořadí příchodu', () => {
    // Ranní retro pass dožene noční frontu — staré zprávy nesmí skončit nahoře
    const feed = mergeNewsRow(
      mergeNewsRow([], row(1, '2026-07-28T12:00:00Z')),
      row(2, '2026-07-28T03:00:00Z'),
    )

    expect(feed.map((item) => item.id)).toEqual([1, 2])
  })

  test('feed nepřeteče přes strop', () => {
    let feed: NewsRow[] = []
    for (let index = 0; index < 250; index += 1) {
      const minute = String(index % 60).padStart(2, '0')
      feed = mergeNewsRow(feed, row(index, `2026-07-28T12:${minute}:00Z`))
    }
    expect(feed).toHaveLength(200)
  })
})
