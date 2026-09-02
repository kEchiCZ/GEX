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

// ── Řada indexu per zobrazený den (#976) ─────────────────────────────

import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, vi } from 'vitest'
import { createElement } from 'react'
import type { ReactNode } from 'react'
import { AppStateProvider } from '../state/AppState'
import type { LiveSocket } from '../api/ws'
import { useNews } from './useNews'

function fakeSocket(): LiveSocket {
  return {
    subscribe: () => {},
    unsubscribe: () => {},
    onReconnect: () => () => {},
    onNotice: () => () => {},
    connect: () => {},
    close: () => {},
  } as unknown as LiveSocket
}

function wrapper({ children }: { children: ReactNode }) {
  return createElement(AppStateProvider, { socket: fakeSocket(), symbol: 'ES', children })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useNews — řada SentIndexu', () => {
  test('žádá index pro zobrazený den, ne pro dnešek', async () => {
    // Bez `date` API vrátí dnešní UTC partici; při replay včerejška by se
    // dnešní ranní hodnoty přilepily (podle popisku HH:MM) na včerejší ráno.
    const fetchMock = vi.fn(async (url: unknown) => {
      const target = String(url)
      if (target.includes('/sentiment/index/')) {
        return {
          ok: true,
          json: async () => ({ series: [{ ts_min: '2026-09-01T07:00:00+00:00', value: 0.4 }] }),
        }
      }
      return { ok: true, json: async () => ({}) }
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result, rerender } = renderHook(({ date }: { date: string }) => useNews(date), {
      wrapper,
      initialProps: { date: '2026-09-01' },
    })
    await waitFor(() => expect(result.current.series).toHaveLength(1))
    const indexCalls = () =>
      fetchMock.mock.calls.map(([u]) => String(u)).filter((u) => u.includes('/sentiment/index/'))
    expect(indexCalls().at(-1)).toContain('/sentiment/index/ES?date=2026-09-01')

    // Přepnutí dne → nový fetch s novým datem
    await act(async () => {
      rerender({ date: '2026-08-31' })
    })
    await waitFor(() => expect(indexCalls().at(-1)).toContain('?date=2026-08-31'))
  })
})
