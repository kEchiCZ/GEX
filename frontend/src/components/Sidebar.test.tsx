/** Testy resynchronizace watchlistu (#407): retry po výpadku, 409 → resync, chybové hlášky. */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { LiveSocket } from '../api/ws'
import { AppStateProvider } from '../state/AppState'
import { FakeWebSocket } from '../test/fakeWs'
import { Sidebar } from './Sidebar'

function renderSidebar() {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  return render(
    <AppStateProvider socket={socket}>
      <Sidebar />
    </AppStateProvider>,
  )
}

/** Fetch mock: /watchlist obsluhuje `handler` (počítá volání), ostatní routy generický OK. */
function mockFetch(handler: (call: number) => Promise<unknown>) {
  let calls = 0
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown, init?: { method?: string }) => {
      const target = String(url)
      if (target.includes('/watchlist')) {
        calls += 1
        return handler(calls) as never
      }
      void init
      return { ok: true, json: async () => ({}) } as never
    }),
  )
}

beforeEach(() => {
  FakeWebSocket.reset()
  window.localStorage.clear()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

test('watchlist se po výpadku API načte znovu (retry), ne až po reloadu (#407)', async () => {
  vi.useFakeTimers()
  mockFetch(async (call) => {
    // První pokus spadne (API se zrovna restartuje), retry už projde
    if (call === 1) throw new Error('connection refused')
    return {
      ok: true,
      json: async () => ({
        watchlist: [
          { id: 1, symbol: 'NQ' },
          { id: 2, symbol: 'ES' },
        ],
      }),
    }
  })
  renderSidebar()
  await act(async () => {
    await vi.advanceTimersByTimeAsync(20_000)
  })
  expect(screen.getByText('NQ')).toBeDefined()
})

test('409 při přidání = symbol na serveru existuje → seznam se srovná se serverem (#407)', async () => {
  mockFetch(async (call) => {
    // 1. GET jen ES; 2. volání je POST 409; 3. GET už vrací i NQ
    if (call === 2) return { ok: false, status: 409, json: async () => ({ detail: 'duplicitní' }) }
    return {
      ok: true,
      json: async () => ({
        watchlist:
          call >= 3
            ? [
                { id: 1, symbol: 'NQ' },
                { id: 2, symbol: 'ES' },
              ]
            : [{ id: 2, symbol: 'ES' }],
      }),
    }
  })
  renderSidebar()
  expect(await screen.findByText('ES')).toBeDefined()

  fireEvent.change(screen.getByLabelText('Nový symbol'), { target: { value: 'NQ' } })
  fireEvent.submit(screen.getByLabelText('Watchlist').querySelector('form')!)
  expect(await screen.findByText('NQ')).toBeDefined()
  expect(screen.queryByRole('alert')).toBeNull()
})

test('nedostupné API při přidání ukáže chybovou hlášku místo tichého selhání (#407)', async () => {
  mockFetch(async (call) => {
    if (call === 1) return { ok: true, json: async () => ({ watchlist: [] }) }
    throw new Error('connection refused')
  })
  renderSidebar()
  expect(await screen.findByLabelText('Watchlist')).toBeDefined()

  fireEvent.change(screen.getByLabelText('Nový symbol'), { target: { value: 'NQ' } })
  fireEvent.submit(screen.getByLabelText('Watchlist').querySelector('form')!)
  expect(await screen.findByRole('alert')).toBeDefined()
})
