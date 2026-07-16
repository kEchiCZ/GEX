/** Testy provozních obrazovek (issue #29): navigace, alerty, settings bez restartu, konzole. */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'

function mockApi() {
  const fetchMock = vi.fn(async (url: unknown, init?: RequestInit) => {
    const target = String(url)
    if (init?.method === 'PUT') {
      return { ok: true, json: async () => ({}) }
    }
    if (target.includes('/expiries')) {
      return { ok: true, json: async () => ({ expiries: ['20260716'] }) }
    }
    if (target.includes('/watchlist')) {
      return {
        ok: true,
        json: async () => ({
          watchlist: [
            { id: 1, symbol: 'ES' },
            { id: 2, symbol: 'SPY' },
          ],
        }),
      }
    }
    if (target.includes('/settings')) {
      return { ok: true, json: async () => ({ settings: { ibkr_port: 7496 } }) }
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

test('navigace v sidebaru přepíná obrazovky', async () => {
  mockApi()
  renderApp()

  fireEvent.click(screen.getByRole('button', { name: 'Dashboard' }))
  expect(await screen.findByLabelText('Karta ES')).toBeDefined()
  expect(screen.getByLabelText('Karta SPY')).toBeDefined()

  fireEvent.click(screen.getByRole('button', { name: 'IBKR Console' }))
  expect(screen.getByLabelText('Připojení')).toBeDefined()

  fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
  expect(screen.getByLabelText('Settings')).toBeDefined()

  fireEvent.click(screen.getByRole('button', { name: 'Graf' }))
  expect(screen.getByLabelText('Heatmapa')).toBeDefined()
})

test('notifikační zvonek: badge z alerts kanálu, otevření ukáže historii a vynuluje badge', () => {
  mockApi()
  renderApp()
  const ws = FakeWebSocket.latest()

  act(() => {
    ws.open()
    ws.push('alerts', { kind: 'price_cross', symbol: 'ES', message: 'cena protnula flip', ts: 1 })
    ws.push('alerts', { kind: 'disk_limit', symbol: '*', message: 'disk limit', ts: 2 })
  })

  const bell = screen.getByLabelText('Notifikace (2)')
  fireEvent.click(bell)
  const history = screen.getByRole('dialog', { name: 'Historie alertů' })
  expect(history.textContent).toContain('cena protnula flip')
  expect(history.textContent).toContain('disk limit')
  expect(screen.getByLabelText('Notifikace (0)')).toBeDefined() // badge vynulován
})

test('změna nastavení se ukládá okamžitě (PUT, bez restartu) a téma se aplikuje živě', async () => {
  const fetchMock = mockApi()
  renderApp()

  fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
  const hotZone = await screen.findByLabelText('Šířka hot zóny (± strikes)')
  fireEvent.change(hotZone, { target: { value: '9' } })

  await waitFor(() => {
    const putCall = fetchMock.mock.calls.find(
      ([url, init]) => init?.method === 'PUT' && String(url).endsWith('/settings/hot_zone_width'),
    )
    expect(putCall).toBeDefined()
    expect(JSON.parse(String(putCall![1]!.body))).toEqual({ value: 9 })
  })

  // Téma: select přepne data-theme okamžitě (bez reloadu) a uloží se na server
  fireEvent.change(screen.getByLabelText('Téma'), { target: { value: 'light' } })
  expect(document.querySelector('.app')?.getAttribute('data-theme')).toBe('light')
  await waitFor(() => {
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) => init?.method === 'PUT' && String(url).endsWith('/settings/theme'),
      ),
    ).toBe(true)
  })
})

test('konzole loguje události ze status kanálu a reconnect zapisuje požadavek', async () => {
  const fetchMock = mockApi()
  renderApp()
  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('status', { engine: 'online', connection: 'connected', port: 7496 })
  })

  fireEvent.click(screen.getByRole('button', { name: 'IBKR Console' }))
  const log = screen.getByLabelText('Log API událostí')
  expect(log.textContent).toContain('status: engine=online')

  fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }))
  await waitFor(() => {
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          init?.method === 'PUT' && String(url).endsWith('/settings/reconnect_requested'),
      ),
    ).toBe(true)
  })
})

test('sidebar obsahuje odkaz na uživatelský manuál (wiki)', () => {
  mockApi()
  renderApp()
  const link = screen.getByRole('link', { name: 'Manuál' }) as HTMLAnchorElement
  expect(link.getAttribute('href')).toBe('/manual/')
  expect(link.target).toBe('_blank')
})

test('přepnutí tématu v sidebaru funguje také (Theme tlačítko)', () => {
  mockApi()
  renderApp()
  expect(document.querySelector('.app')?.getAttribute('data-theme')).toBe('dark')
  fireEvent.click(screen.getByRole('button', { name: 'Theme: Dark' }))
  expect(document.querySelector('.app')?.getAttribute('data-theme')).toBe('light')
})
