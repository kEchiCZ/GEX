/** Testy Greeks & OI tabulky (#202): načtení řetězu, C/P strany, ATM zvýraznění. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'

const CHAIN_PAYLOAD = {
  ts: '2026-07-16T15:02:00+00:00',
  symbol: 'ES',
  expiry: '20260717',
  rows: [
    {
      strike: 7590,
      call: side({ bid: 10, oi: 100, oi_change: 20 }),
      put: side({ bid: 8, oi: 150, oi_change: null, stale: true }),
    },
    { strike: 7600, call: side({ bid: 6, oi: 200, oi_change: -5 }), put: side({ bid: 9, oi: 250, oi_change: 10 }) }, // prettier-ignore
  ],
}

function side(overrides: Record<string, unknown>) {
  return {
    bid: 1,
    ask: 1.5,
    last: 1.2,
    volume: 42,
    iv: 0.15,
    delta: 0.5,
    gamma: 0.01,
    theta: -0.5,
    vega: 1.2,
    oi: 0,
    oi_change: null,
    stale: false,
    ...overrides,
  }
}

function mockApi() {
  const fetchMock = vi.fn(async (url: unknown) => {
    const target = String(url)
    if (target.includes('/chain/')) {
      return { ok: true, json: async () => CHAIN_PAYLOAD }
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

beforeEach(() => {
  FakeWebSocket.reset()
  vi.restoreAllMocks()
  localStorage.clear()
})

test('obrazovka Řetěz načte chain a vykreslí C/P strany se strikem uprostřed (#202)', async () => {
  mockApi()
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(<App socket={socket} />)

  fireEvent.click(screen.getByRole('button', { name: 'Řetěz' }))
  await waitFor(() => {
    expect(screen.getByLabelText('Greeks & OI tabulka')).toBeTruthy()
    expect(screen.getByText('7590')).toBeTruthy()
  })
  // Nejvyšší strike nahoře (orientace heatmapy)
  const strikes = [...document.querySelectorAll('tbody td.strike-col')].map(
    (cell) => cell.textContent,
  )
  expect(strikes).toEqual(['7600', '7590'])
  // ΔOI se znaménkem; stale strana ztlumená přes třídu
  expect(screen.getByText('+20')).toBeTruthy()
  expect(document.querySelectorAll('td.stale').length).toBeGreaterThan(0)
})
