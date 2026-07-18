/** Testy setup detektoru v UI (ADR-0004): obrazovka Setupy, hodnocení, WS refresh. */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { setupRrr } from '../api/setups'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'

const SETUP_ROW = {
  id: 7,
  symbol: 'ES',
  expiry: '20260717',
  template: 'failed_break',
  direction: 'long',
  created_ts: '2026-07-17T15:02:00+00:00',
  entry: 7501,
  target: 7515,
  stop: 7472,
  confidence: 55,
  reason: 'Neúspěšný průraz 7500 dolů (dno 7473 bez akceptace) a reclaim — spring.',
  status: 'closed_target',
  closed_ts: '2026-07-17T15:40:00+00:00',
  outcome_r: 0.48,
  mfe: 15,
  mae: 6,
  user_rating: null,
  user_note: null,
}

function mockApi(setups: Array<Record<string, unknown>>) {
  const fetchMock = vi.fn(async (url: unknown, init?: RequestInit) => {
    const target = String(url)
    if (init?.method === 'PATCH' && target.includes('/review')) {
      return { ok: true, json: async () => ({ status: 'ok' }) }
    }
    if (target.includes('/setups/')) {
      return { ok: true, json: async () => ({ symbol: 'ES', setups }) }
    }
    if (target.includes('/expiries')) {
      return { ok: true, json: async () => ({ expiries: ['20260717'] }) }
    }
    if (target.includes('/watchlist')) {
      return { ok: true, json: async () => ({ watchlist: [{ id: 1, symbol: 'ES' }] }) }
    }
    if (target.includes('/annotations')) {
      return { ok: true, json: async () => ({ annotations: [] }) }
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
})

test('výpočet RRR ze setupu', () => {
  expect(setupRrr({ entry: 7501, target: 7515, stop: 7472 })).toBeCloseTo(14 / 29)
  expect(setupRrr({ entry: 7501, target: 7515, stop: 7501 })).toBe(0)
})

test('obrazovka Setupy: historie s výsledkem a hodnocením', async () => {
  const fetchMock = mockApi([SETUP_ROW])
  renderApp()

  fireEvent.click(screen.getByRole('button', { name: 'Setupy' }))
  expect(await screen.findByText('Neúspěšný průraz')).toBeDefined()
  expect(screen.getByText('+0.48')).toBeDefined()
  // 'Cíl' je hlavička sloupce i badge stavu — badge přidává druhý výskyt
  expect(screen.getAllByText('Cíl').length).toBe(2)

  // Ruční hodnocení: 👍 pošle PATCH na /setups/ES/7/review
  fireEvent.click(screen.getByRole('button', { name: 'Setup 7 vyšel' }))
  await waitFor(() => {
    const patch = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit | undefined)?.method === 'PATCH',
    )
    expect(patch).toBeDefined()
    expect(String(patch?.[0])).toContain('/setups/ES/7/review')
    expect(JSON.parse(String((patch?.[1] as RequestInit).body))).toEqual({
      rating: 1,
      note: null,
    })
  })
})

test('aktivní setup: karta nad grafem s úrovněmi a skrytím', async () => {
  mockApi([{ ...SETUP_ROW, status: 'active', closed_ts: null, outcome_r: null }])
  renderApp()

  expect(await screen.findByLabelText('Aktivní setupy')).toBeDefined()
  expect(screen.getByText('Entry 7501')).toBeDefined()
  expect(screen.getByText('Cíl 7515')).toBeDefined()
  expect(screen.getByText('Stop 7472')).toBeDefined()

  fireEvent.click(screen.getByRole('button', { name: 'Skrýt setup 7' }))
  expect(screen.queryByLabelText('Aktivní setupy')).toBeNull()
})

test('WS událost setups.* přenačte setupy', async () => {
  const fetchMock = mockApi([])
  renderApp()
  await waitFor(() => {
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/setups/ES'))).toBe(true)
  })
  const before = fetchMock.mock.calls.filter(([url]) => String(url).includes('/setups/ES')).length

  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('setups.ES', { event: 'created', id: 9 })
  })
  await waitFor(() => {
    const after = fetchMock.mock.calls.filter(([url]) => String(url).includes('/setups/ES')).length
    expect(after).toBeGreaterThan(before)
  })
})
