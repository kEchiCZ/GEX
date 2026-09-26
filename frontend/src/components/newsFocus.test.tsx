/** Proklik z upozornění na zprávy (#1290): zvoneček → graf s dialogem zpráv. */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'

const ALERT_ROWS = [
  {
    id: 11,
    ts_event: '2026-09-25T11:00:20+00:00',
    kind: 'scheduled',
    category: 'MACRO_INFLATION',
    importance: 3,
    title: 'USD Core PCE Price Index m/m',
    summary: null,
    sentiment_dir: -1,
    sentiment_score: -0.6,
    forecast: 0.2,
    previous: 0.3,
    actual: 0.4,
    surprise_z: 1.8,
  },
  {
    id: 12,
    ts_event: '2026-09-25T11:01:05+00:00',
    kind: 'headline',
    category: 'FED',
    importance: 2,
    title: 'Fed Waller: inflace se vrací',
    summary: null,
    sentiment_dir: -1,
    sentiment_score: -0.4,
    forecast: null,
    previous: null,
    actual: null,
    surprise_z: null,
  },
]

function mockApi() {
  const fetchMock = vi.fn(async (url: unknown) => {
    const target = String(url)
    if (target.includes('/news/markers?ids=')) {
      return { ok: true, status: 200, json: async () => ({ news: ALERT_ROWS }) }
    }
    if (target.includes('/news/markers')) {
      return { ok: true, status: 200, json: async () => ({ news: [] }) }
    }
    if (target.includes('/expiries')) {
      return { ok: true, json: async () => ({ expiries: ['20260925'] }) }
    }
    return { ok: false, status: 404, json: async () => ({}) }
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderApp() {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  return render(<App socket={socket} />)
}

beforeEach(() => {
  FakeWebSocket.reset()
  vi.restoreAllMocks()
  localStorage.clear()
})

test('upozornění na zprávy s event_ids otevře dialog se zprávami v grafu (#1290)', async () => {
  const fetchMock = mockApi()
  renderApp()
  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('alerts', {
      kind: 'news_anomaly',
      symbol: 'NQ',
      message: 'Reakce NQ na zprávy z 13:00: ↓ -32 bp za 5 min',
      ts: 1790334300,
      ts_event: '2026-09-25T11:00:20+00:00',
      event_ids: [11, 12],
    })
  })
  fireEvent.click(screen.getByRole('button', { name: /Notifikace/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Otevřít zprávy NQ v grafu' }))

  const dialog = await screen.findByRole('dialog', { name: 'Zprávy v čase markeru' })
  expect(dialog.textContent).toContain('USD Core PCE Price Index m/m')
  expect(dialog.textContent).toContain('Fed Waller: inflace se vrací')
  const idsCall = fetchMock.mock.calls
    .map(([url]) => String(url))
    .find((url) => url.includes('ids='))
  expect(idsCall).toContain('/news/markers?ids=11,12')
  // Proklik přepne na instrument upozornění a zapne vrstvu News (marker musí být vidět)
  await waitFor(() =>
    expect((screen.getByRole('checkbox', { name: 'News' }) as HTMLInputElement).checked).toBe(true),
  )
})

test('upozornění bez event_ids (starší payload) zůstává prostý text', () => {
  mockApi()
  renderApp()
  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('alerts', {
      kind: 'news_anomaly',
      symbol: 'ES',
      message: 'Reakce ES na zprávy z 13:00',
      ts: 1790334300,
    })
  })
  fireEvent.click(screen.getByRole('button', { name: /Notifikace/ }))
  expect(screen.queryByRole('button', { name: /Otevřít zprávy/ })).toBeNull()
  expect(screen.getByRole('dialog', { name: 'Historie alertů' }).textContent).toContain(
    'Reakce ES na zprávy z 13:00',
  )
})

test('chyba načtení zpráv upozornění je vidět, ne tichý prázdný dialog', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      const target = String(url)
      if (target.includes('/news/markers'))
        return { ok: false, status: 500, json: async () => ({}) }
      if (target.includes('/expiries')) {
        return { ok: true, json: async () => ({ expiries: ['20260925'] }) }
      }
      return { ok: false, status: 404, json: async () => ({}) }
    }),
  )
  renderApp()
  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('alerts', {
      kind: 'news_preopen',
      symbol: 'ES',
      message: 'Před otevřením ES',
      ts: 1790334300,
      ts_event: '2026-09-27T22:00:00+00:00',
      event_ids: [5],
    })
  })
  fireEvent.click(screen.getByRole('button', { name: /Notifikace/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Otevřít zprávy ES v grafu' }))
  const banner = await screen.findByTestId('news-load-error')
  expect(banner.textContent).toContain('HTTP 500')
})

test('upozornění před releasem (#1296) je ve zvonečku proklikávací na zprávy releasu', async () => {
  const fetchMock = mockApi()
  renderApp()
  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('alerts', {
      kind: 'release_preview',
      symbol: 'ES',
      message: 'PCE za 15 min (13:00) — ES 7805.5',
      ts: 1790334300,
      ts_event: '2026-09-25T11:00:00+00:00',
      event_ids: [11],
    })
  })
  fireEvent.click(screen.getByRole('button', { name: /Notifikace/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Otevřít zprávy ES v grafu' }))
  const dialog = await screen.findByRole('dialog', { name: 'Zprávy v čase markeru' })
  expect(dialog.textContent).toContain('USD Core PCE Price Index m/m')
  const idsCall = fetchMock.mock.calls
    .map(([url]) => String(url))
    .find((url) => url.includes('ids='))
  expect(idsCall).toContain('/news/markers?ids=11')
})
