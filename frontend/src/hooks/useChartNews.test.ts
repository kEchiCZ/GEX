/** Testy načítání zpráv grafu po seancích (#1290): URL dne, cache, WS upsert,
dotažení konce dne, reconnect a viditelná chyba. */
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import type { ChartNewsRow } from '../api/news'
import type { LiveSocket } from '../api/ws'
import {
  CHART_NEWS_RETRY_MS,
  CHART_NEWS_TAIL_MS,
  CHART_NEWS_TAIL_WINDOW_MS,
  CHART_NEWS_WS_FLUSH_MS,
  mergeRestRows,
  useChartNews,
} from './useChartNews'
import type { ChartNewsDays } from './useChartNews'

const VIEW_DATE = '2026-09-16'
/** Seance 16. 9. (CDT): [15. 9. 22:00Z, 16. 9. 22:00Z). */
const OPEN_ISO = '2026-09-15T22:00:00.000Z'
const CLOSE_ISO = '2026-09-16T22:00:00.000Z'
/** „Teď" uprostřed seance. */
const NOW = Date.parse('2026-09-16T15:00:00Z')

function newsRow(id: number, ts: string, extra: Partial<ChartNewsRow> = {}): ChartNewsRow {
  return {
    id,
    ts_event: ts,
    kind: 'headline',
    category: 'OTHER',
    importance: 1,
    title: `Zpráva ${id}`,
    summary: null,
    sentiment_dir: null,
    sentiment_score: null,
    forecast: null,
    previous: null,
    actual: null,
    ...extra,
  }
}

type Handler = (data: Record<string, unknown>) => void

function fakeSocket() {
  const handlers = new Set<Handler>()
  const reconnects = new Set<() => void>()
  const socket = {
    subscribe: (channel: string, handler: Handler) => {
      if (channel === 'news') handlers.add(handler)
    },
    unsubscribe: (_channel: string, handler: Handler) => handlers.delete(handler),
    onReconnect: (handler: () => void) => {
      reconnects.add(handler)
      return () => reconnects.delete(handler)
    },
  } as unknown as LiveSocket
  return {
    socket,
    push: (data: Record<string, unknown>) => handlers.forEach((handler) => handler(data)),
    reconnect: () => reconnects.forEach((handler) => handler()),
  }
}

/** fetch mock: odpověď podle rozsahu `from/to` (nebo chybový status). */
function mockFetch(respond: (from: string, to: string) => ChartNewsRow[] | number) {
  const calls: { from: string; to: string }[] = []
  const fetchMock = vi.fn(async (url: unknown) => {
    const params = new URL(String(url), 'http://api').searchParams
    const from = params.get('from') ?? ''
    const to = params.get('to') ?? ''
    calls.push({ from, to })
    const result = respond(from, to)
    if (typeof result === 'number') return { ok: false, status: result, json: async () => ({}) }
    return { ok: true, status: 200, json: async () => ({ news: result }) }
  })
  vi.stubGlobal('fetch', fetchMock)
  return calls
}

/** Doběhnutí promise řetězců a časovačů bez reálného čekání. */
async function flush(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW)
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('useChartNews', () => {
  test('den = jeden request s hranicemi seance, historie po jednom a jen jednou', async () => {
    const calls = mockFetch((from) =>
      from === OPEN_ISO ? [newsRow(1, '2026-09-16T13:00:00Z')] : [],
    )
    const { result, rerender } = renderHook(
      ({ history }: { history: string[] }) =>
        useChartNews({ enabled: true, viewDate: VIEW_DATE, live: false, historyDates: history }),
      { initialProps: { history: ['2026-09-15', '2026-09-14'] } },
    )
    await flush()
    await flush()
    await flush()
    expect(calls).toEqual([
      { from: OPEN_ISO, to: CLOSE_ISO },
      { from: '2026-09-14T22:00:00.000Z', to: '2026-09-15T22:00:00.000Z' },
      { from: '2026-09-13T22:00:00.000Z', to: '2026-09-14T22:00:00.000Z' },
    ])
    expect(result.current.days.get(VIEW_DATE)?.get(1)?.tsMs).toBe(
      Date.parse('2026-09-16T13:00:00Z'),
    )

    // Nová identita téhož seznamu (re-render App, přepnutí ES↔NQ) nic nestahuje
    rerender({ history: ['2026-09-15', '2026-09-14'] })
    await flush(CHART_NEWS_TAIL_MS)
    expect(calls).toHaveLength(3)
  })

  test('vypnuté markery nic nestahují', async () => {
    const calls = mockFetch(() => [])
    renderHook(() =>
      useChartNews({ enabled: false, viewDate: VIEW_DATE, live: true, historyDates: [] }),
    )
    await flush(CHART_NEWS_TAIL_MS * 2)
    expect(calls).toHaveLength(0)
  })

  test('WS upsert bez stropu 200 a bez duplicit podle id', async () => {
    mockFetch(() => [newsRow(1, '2026-09-16T10:00:00Z')])
    const ws = fakeSocket()
    const { result } = renderHook(() =>
      useChartNews({
        enabled: true,
        viewDate: VIEW_DATE,
        live: true,
        historyDates: [],
        socket: ws.socket,
      }),
    )
    await flush()
    act(() => {
      for (let index = 0; index < 250; index += 1) {
        ws.push(newsRow(1000 + index, '2026-09-16T14:30:00Z') as unknown as Record<string, unknown>)
      }
      // Tatáž zpráva podruhé (klasifikovaná verze) nesmí přidat řádek
      ws.push({ ...newsRow(1000, '2026-09-16T14:30:00Z'), category: 'FED' })
      // Provozní hláška kanálu bez id se ignoruje
      ws.push({ kind: 'retro_pass', message: 'hotovo' })
    })
    await flush(CHART_NEWS_WS_FLUSH_MS)
    const day = result.current.days.get(VIEW_DATE)
    expect(day?.size).toBe(251)
    expect(day?.get(1000)?.category).toBe('FED')
  })

  test('WS null nepřepíše čísla makra načtená z REST', async () => {
    mockFetch(() => [
      newsRow(7, '2026-09-16T12:30:00Z', {
        kind: 'scheduled',
        forecast: 2.9,
        previous: 3.0,
        actual: 2.7,
        surprise_z: -1.4,
      }),
    ])
    const ws = fakeSocket()
    const { result } = renderHook(() =>
      useChartNews({
        enabled: true,
        viewDate: VIEW_DATE,
        live: true,
        historyDates: [],
        socket: ws.socket,
      }),
    )
    await flush()
    act(() => {
      // news-engine pushuje klasifikaci vždy s forecast/previous/actual = null
      ws.push({
        ...newsRow(7, '2026-09-16T12:30:00Z', { kind: 'scheduled', category: 'MACRO_INFLATION' }),
        sentiment_dir: 1,
      })
    })
    await flush(CHART_NEWS_WS_FLUSH_MS)
    const row = result.current.days.get(VIEW_DATE)?.get(7)
    expect(row?.actual).toBe(2.7)
    expect(row?.forecast).toBe(2.9)
    expect(row?.surprise_z).toBe(-1.4)
    expect(row?.category).toBe('MACRO_INFLATION')
    expect(row?.sentiment_dir).toBe(1)
  })

  test('živý den: po minutě dotažení od posledního úspěšného − 30 min', async () => {
    const calls = mockFetch((from) =>
      from === OPEN_ISO ? [] : [newsRow(5, '2026-09-16T14:59:00Z', { actual: 1.2 })],
    )
    const { result } = renderHook(() =>
      useChartNews({ enabled: true, viewDate: VIEW_DATE, live: true, historyDates: [] }),
    )
    await flush()
    expect(calls).toHaveLength(1)
    await flush(CHART_NEWS_TAIL_MS)
    expect(calls).toHaveLength(2)
    // Celý den stažený v NOW → dotažení od NOW − 30 min do konce seance
    expect(Date.parse(calls[1].from)).toBe(NOW - CHART_NEWS_TAIL_WINDOW_MS)
    expect(calls[1].to).toBe(CLOSE_ISO)
    expect(result.current.days.get(VIEW_DATE)?.get(5)?.actual).toBe(1.2)
    // Další dotažení navazuje na předchozí úspěšné
    await flush(CHART_NEWS_TAIL_MS)
    expect(Date.parse(calls[2].from)).toBe(NOW + CHART_NEWS_TAIL_MS - CHART_NEWS_TAIL_WINDOW_MS)
  })

  test('výpadek REST delší než okno se po obnově zacelí celý', async () => {
    let failing = false
    const calls = mockFetch(() => (failing ? 502 : []))
    const { result } = renderHook(() =>
      useChartNews({ enabled: true, viewDate: VIEW_DATE, live: true, historyDates: [] }),
    )
    await flush()
    failing = true
    await flush(45 * 60_000) // 45 dotažení s chybou
    expect(result.current.error).toContain('dotažení: news/markers: HTTP 502')
    failing = false
    await flush(CHART_NEWS_TAIL_MS)
    // Od posledního úspěšného (NOW) s rezervou, ne jen posledních 30 min
    expect(Date.parse(calls[calls.length - 1].from)).toBe(NOW - CHART_NEWS_TAIL_WINDOW_MS)
    // Úspěšné dotažení chybu smaže — hláška nesmí viset (#1290 review)
    expect(result.current.error).toBeNull()
  })

  test('dotažení beze změny drží identitu dnů — žádný re-render grafu (#1274)', async () => {
    const rows = [newsRow(1, '2026-09-16T14:50:00Z'), newsRow(2, '2026-09-16T14:55:00Z')]
    mockFetch(() => rows)
    const { result } = renderHook(() =>
      useChartNews({ enabled: true, viewDate: VIEW_DATE, live: true, historyDates: [] }),
    )
    await flush()
    const before = result.current.days
    const nowBefore = result.current.nowMs
    await flush(CHART_NEWS_TAIL_MS)
    expect(result.current.days).toBe(before)
    // Tik bez vydaného plánovaného eventu „teď" neposouvá
    expect(result.current.nowMs).toBe(nowBefore)
  })

  test('mergeRestRows: změněný řádek kopíruje den, nezměněný vrací původní mapu', () => {
    const ts = '2026-09-16T14:50:00Z'
    const days: ChartNewsDays = new Map([
      [VIEW_DATE, new Map([[1, { ...newsRow(1, ts), tsMs: Date.parse(ts) }]])],
    ])
    expect(mergeRestRows(days, VIEW_DATE, [newsRow(1, ts)])).toBe(days)
    const changed = mergeRestRows(days, VIEW_DATE, [newsRow(1, ts, { category: 'FED' })])
    expect(changed).not.toBe(days)
    expect(changed.get(VIEW_DATE)?.get(1)?.category).toBe('FED')
  })

  test('plánovaný event, který přešel do minulosti, posune „teď" (tik i WS)', async () => {
    mockFetch(() => [newsRow(9, '2026-09-16T15:00:30Z', { kind: 'scheduled' })])
    const ws = fakeSocket()
    const { result } = renderHook(() =>
      useChartNews({
        enabled: true,
        viewDate: VIEW_DATE,
        live: true,
        historyDates: [],
        socket: ws.socket,
      }),
    )
    await flush()
    expect(result.current.nowMs).toBe(NOW)
    await flush(CHART_NEWS_TAIL_MS)
    // 15:00:30 je mezi NOW a NOW + 60 s → „teď" se posune, CPI už není nadcházející
    expect(result.current.nowMs).toBe(NOW + CHART_NEWS_TAIL_MS)
    // WS: vydaný plánovaný event (výsledek) posune „teď" hned s dávkou
    const released = '2026-09-16T15:01:20Z'
    await flush(20_000)
    act(() => ws.push({ ...newsRow(10, released, { kind: 'scheduled' }) }))
    await flush(CHART_NEWS_WS_FLUSH_MS)
    expect(result.current.nowMs).toBeGreaterThanOrEqual(Date.parse(released))
  })

  test('opakovaný WS push téže verze nemění identitu dnů', async () => {
    const row = newsRow(3, '2026-09-16T14:30:00Z')
    mockFetch(() => [row])
    const ws = fakeSocket()
    const { result } = renderHook(() =>
      useChartNews({
        enabled: true,
        viewDate: VIEW_DATE,
        live: true,
        historyDates: [],
        socket: ws.socket,
      }),
    )
    await flush()
    const before = result.current.days
    act(() => ws.push({ ...row }))
    await flush(CHART_NEWS_WS_FLUSH_MS)
    expect(result.current.days).toBe(before)
  })

  test('vypnutí a zapnutí vrstvy: živý den se stáhne celý znovu (díra z pauzy)', async () => {
    const calls = mockFetch(() => [])
    const ws = fakeSocket()
    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) =>
        useChartNews({
          enabled,
          viewDate: VIEW_DATE,
          live: true,
          historyDates: [],
          socket: ws.socket,
        }),
      { initialProps: { enabled: true } },
    )
    await flush()
    expect(calls).toHaveLength(1)
    // Vypnuto, zapnuto o 2 h později — WS ani dotažení mezitím nešly
    rerender({ enabled: false })
    await flush(2 * 3_600_000)
    expect(calls).toHaveLength(1)
    rerender({ enabled: true })
    await flush()
    expect(calls).toEqual([
      { from: OPEN_ISO, to: CLOSE_ISO },
      { from: OPEN_ISO, to: CLOSE_ISO },
    ])
  })

  test('návrat z jiného (historického) dne na živý den ho stáhne celý znovu', async () => {
    const calls = mockFetch(() => [])
    const { rerender } = renderHook(
      ({ date, live }: { date: string; live: boolean }) =>
        useChartNews({ enabled: true, viewDate: date, live, historyDates: [] }),
      { initialProps: { date: VIEW_DATE, live: true } },
    )
    await flush()
    rerender({ date: '2026-09-15', live: false })
    await flush(3_600_000)
    rerender({ date: VIEW_DATE, live: true })
    await flush()
    const todayCalls = calls.filter((call) => call.from === OPEN_ISO && call.to === CLOSE_ISO)
    expect(todayCalls).toHaveLength(2)
  })

  test('reconnect WS stáhne celý živý den znovu', async () => {
    const calls = mockFetch(() => [])
    const ws = fakeSocket()
    renderHook(() =>
      useChartNews({
        enabled: true,
        viewDate: VIEW_DATE,
        live: true,
        historyDates: [],
        socket: ws.socket,
      }),
    )
    await flush()
    expect(calls).toHaveLength(1)
    act(() => ws.reconnect())
    await flush()
    expect(calls).toEqual([
      { from: OPEN_ISO, to: CLOSE_ISO },
      { from: OPEN_ISO, to: CLOSE_ISO },
    ])
  })

  test('chyba načtení je vidět a po minutě se zkusí znovu', async () => {
    let status: number | null = 503
    const calls = mockFetch(() => status ?? [newsRow(1, '2026-09-16T13:00:00Z')])
    const { result } = renderHook(() =>
      useChartNews({ enabled: true, viewDate: VIEW_DATE, live: false, historyDates: [] }),
    )
    await flush()
    expect(result.current.error).toContain('HTTP 503')
    expect(result.current.error).toContain(VIEW_DATE)
    expect(result.current.days.has(VIEW_DATE)).toBe(false)
    status = null
    await flush(CHART_NEWS_RETRY_MS)
    await flush()
    expect(calls.length).toBe(2)
    expect(result.current.error).toBeNull()
    expect(result.current.days.get(VIEW_DATE)?.size).toBe(1)
  })

  test('chybu zobrazeného dne neschová úspěch historického dne', async () => {
    mockFetch((from) => (from === OPEN_ISO ? 503 : []))
    const { result } = renderHook(() =>
      useChartNews({
        enabled: true,
        viewDate: VIEW_DATE,
        live: false,
        historyDates: ['2026-09-15'],
      }),
    )
    await flush()
    await flush()
    expect(result.current.days.has('2026-09-15')).toBe(true)
    expect(result.current.error).toContain(`${VIEW_DATE}: news/markers: HTTP 503`)
  })

  test('vypnutá vrstva (Daily) chybu nehlásí', async () => {
    mockFetch(() => 503)
    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) =>
        useChartNews({ enabled, viewDate: VIEW_DATE, live: false, historyDates: [] }),
      { initialProps: { enabled: true } },
    )
    await flush()
    expect(result.current.error).not.toBeNull()
    rerender({ enabled: false })
    expect(result.current.error).toBeNull()
  })
})
