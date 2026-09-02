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

test('opakovaný focus při nedostupném API nemnoží retry řetězy (#506)', async () => {
  vi.useFakeTimers()
  let calls = 0
  mockFetch(async (call) => {
    calls = call
    throw new Error('connection refused')
  })
  renderSidebar()
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0) // úvodní sync selže → naplánován retry
  })
  const afterInitial = calls

  // Tři návraty do okna, každý spustí sync (selže) a naplánuje retry —
  // starý čekající timeout se musí zrušit, platí vždy jen jeden řetěz
  await act(async () => {
    fireEvent.focus(window)
    fireEvent.focus(window)
    fireEvent.focus(window)
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(calls).toBe(afterInitial + 3)

  // Po intervalu smí doběhnout JEDEN retry, ne čtyři paralelní
  await act(async () => {
    await vi.advanceTimersByTimeAsync(15_000)
  })
  expect(calls).toBe(afterInitial + 4)
})

test('položka Nedávné jde vyřadit křížkem a vyřazení přežije remount (#987)', async () => {
  // Nedávné = symboly, které uživatel zobrazil (typicky z vyhledávání); bez
  // křížku z panelu nešly dostat — „CL ve watchlistu bez křížku"
  window.localStorage.setItem('gexlens.recentSymbols', JSON.stringify(['ES', 'CL', 'NQ']))
  mockFetch(async () => ({ ok: true, json: async () => ({ watchlist: [] }) }))
  const { unmount } = renderSidebar()
  const recent = await screen.findByTestId('recent-CL')
  expect(recent).toBeDefined()

  fireEvent.click(screen.getByRole('button', { name: 'Vyřadit CL z nedávných' }))
  expect(screen.queryByTestId('recent-CL')).toBeNull()
  expect(screen.getByTestId('recent-NQ')).toBeDefined()

  unmount()
  renderSidebar()
  await screen.findByTestId('recent-NQ')
  expect(screen.queryByTestId('recent-CL')).toBeNull()
})

test('aktivní ad-hoc symbol není ve watchlistu, ale zvýrazněný v Nedávných (#989)', async () => {
  // Dřív se přimíchal jako pseudo-řádek watchlistu bez křížku a po přepnutí
  // jinam zmizel — vypadalo to jako rozbitý watchlist
  window.localStorage.setItem('gexlens.symbol', JSON.stringify('CL'))
  window.localStorage.setItem('gexlens.recentSymbols', JSON.stringify(['CL', 'ES']))
  mockFetch(async () => ({
    ok: true,
    json: async () => ({
      watchlist: [
        { id: 1, symbol: 'NQ' },
        { id: 2, symbol: 'ES' },
      ],
    }),
  }))
  renderSidebar()
  const watchlist = await screen.findByLabelText('Watchlist')
  await screen.findByText('NQ')
  expect(watchlist.textContent).not.toContain('CL')
  // Každá položka watchlistu má křížek
  expect(screen.getByRole('button', { name: 'Odebrat NQ' })).toBeDefined()
  // Nedávné: jen symboly mimo watchlist (ES tam není), aktivní CL zvýrazněný bez křížku
  const recent = screen.getByTestId('recent-CL')
  expect(recent.className).toContain('active')
  expect(screen.queryByTestId('recent-ES')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Vyřadit CL z nedávných' })).toBeNull()

  // Přepnutí na NQ: CL zůstane v Nedávných, teď už s křížkem
  fireEvent.click(screen.getByText('NQ'))
  expect(screen.getByTestId('recent-CL').className).not.toContain('active')
  expect(screen.getByRole('button', { name: 'Vyřadit CL z nedávných' })).toBeDefined()
})
