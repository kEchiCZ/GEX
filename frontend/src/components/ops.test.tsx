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

  fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
  expect(screen.getByLabelText('Settings')).toBeDefined()
  // IBKR Console zrušena (#705) — stav žije v Settings
  expect(screen.queryByRole('button', { name: 'IBKR Console' })).toBeNull()
  expect(screen.getByTestId('engine-status')).toBeDefined()

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

test('nastavení se uloží tlačítkem (PUT, bez restartu) a téma se aplikuje živě', async () => {
  const fetchMock = mockApi()
  renderApp()

  fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
  const batchSize = await screen.findByLabelText('Velikost dávky')
  fireEvent.change(batchSize, { target: { value: '60' } })
  // Do #445 letěl PUT po každém stisku klávesy — rozepsaná hodnota tak stihla
  // dojet do enginu. Nově se odesílá až potvrzením.
  fireEvent.click(screen.getByRole('button', { name: 'Uložit' }))

  await waitFor(() => {
    const putCall = fetchMock.mock.calls.find(
      ([url, init]) => init?.method === 'PUT' && String(url).endsWith('/settings/batch_size'),
    )
    expect(putCall).toBeDefined()
    expect(JSON.parse(String(putCall![1]!.body))).toEqual({ value: 60 })
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

test('Settings nese stav enginu a log událostí (náhrada Console, #705)', async () => {
  mockApi()
  renderApp()
  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('status', { engine: 'online', connection: 'connected', port: 7496 })
  })

  fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
  const block = screen.getByTestId('engine-status')
  expect(block.textContent).toContain('online')
  expect(block.textContent).toContain('7496')
  const log = screen.getByLabelText('Log API událostí')
  expect(log.textContent).toContain('status: engine=online')
  // Tlačítko Reconnect zůstává odstraněné (#554)
  expect(screen.queryByRole('button', { name: 'Reconnect' })).toBeNull()
})

test('řádek Spojení ukazuje délku výpadku IBKR, jen když pole přijde (#770)', async () => {
  mockApi()
  renderApp()
  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('status', {
      engine: 'online',
      connection: 'reconnecting',
      port: 7496,
      connection_offline_for_s: 480,
    })
  })

  fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
  const block = screen.getByTestId('engine-status')
  expect(block.textContent).toContain('bez spojení 8 min')

  // Spojení se vrátí → klíč ze statusu zmizí a údaj o výpadku s ním
  act(() => {
    ws.push('status', { engine: 'online', connection: 'connected', port: 7496 })
  })
  expect(screen.getByTestId('engine-status').textContent).not.toContain('bez spojení')
})

test('sidebar obsahuje odkaz na uživatelský manuál (wiki)', () => {
  mockApi()
  renderApp()
  const link = screen.getByRole('link', { name: 'Manuál' }) as HTMLAnchorElement
  expect(link.getAttribute('href')).toBe('/manual/index.html')
  expect(link.target).toBe('_blank')
})

test('přepnutí tématu v sidebaru funguje také (Theme tlačítko)', () => {
  mockApi()
  renderApp()
  expect(document.querySelector('.app')?.getAttribute('data-theme')).toBe('dark')
  fireEvent.click(screen.getByRole('button', { name: 'Theme: Dark' }))
  expect(document.querySelector('.app')?.getAttribute('data-theme')).toBe('light')
})

// ── Ruční přepojení (#950) ─────────────────────────────────────────

test('přepojení: potvrzení, POST /engine/reconnect a hláška o vyžádání', async () => {
  const fetchMock = mockApi()
  fetchMock.mockImplementation(async (url: unknown) => {
    const target = String(url)
    if (target.includes('/engine/reconnect')) {
      return { ok: true, json: async () => ({ targets: ['ibkr'], requested_at: 1 }) }
    }
    if (target.includes('/settings')) {
      return { ok: true, json: async () => ({ settings: {} }) }
    }
    if (target.includes('/expiries')) {
      return { ok: true, json: async () => ({ expiries: ['20260716'] }) }
    }
    return { ok: true, json: async () => ({ watchlist: [], annotations: [] }) }
  })
  const confirmMock = vi.fn(() => true)
  vi.stubGlobal('confirm', confirmMock)
  renderApp()
  fireEvent.click(screen.getByRole('button', { name: 'Settings' }))

  fireEvent.click(screen.getByTestId('reconnect-ibkr'))

  // Přepojení je ~1–2 min díra ve sběru → nesmí jít spustit bez potvrzení
  expect(confirmMock).toHaveBeenCalledTimes(1)
  await waitFor(() => {
    const calls = fetchMock.mock.calls.filter(([u]) => String(u).includes('/engine/reconnect'))
    expect(calls).toHaveLength(1)
    expect(calls[0][1]).toMatchObject({ method: 'POST' })
    expect(JSON.parse(String((calls[0][1] as RequestInit).body))).toEqual({ target: 'ibkr' })
  })
  expect(screen.getByTestId('engine-status').textContent).toContain('vyžádáno')
})

test('přepojení: zamítnuté potvrzení nic neposílá', () => {
  const fetchMock = mockApi()
  vi.stubGlobal(
    'confirm',
    vi.fn(() => false),
  )
  renderApp()
  fireEvent.click(screen.getByRole('button', { name: 'Settings' }))

  fireEvent.click(screen.getByTestId('reconnect-ibkr'))

  expect(
    fetchMock.mock.calls.filter(([u]) => String(u).includes('/engine/reconnect')),
  ).toHaveLength(0)
})

// ── Doskok na starší seanci musí být vidět (#946) ──────────────────

function mockApiSDny(days: { date: string; expiry: string }[]) {
  const fetchMock = vi.fn(async (url: unknown) => {
    const target = String(url)
    if (target.includes('/days')) return { ok: true, json: async () => ({ days }) }
    if (target.includes('/expiries')) {
      return { ok: true, json: async () => ({ expiries: ['20260828', '20260831'] }) }
    }
    if (target.includes('/watchlist')) {
      return { ok: true, json: async () => ({ watchlist: [{ id: 1, symbol: 'ES' }] }) }
    }
    return { ok: true, json: async () => ({ annotations: [], settings: {} }) }
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

test('doskok na starší seanci se ohlásí bannerem (#946)', async () => {
  // Pro vybranou expiraci (20260831) nejsou dny → doskočí na 20260828.
  // O víkendu je to čekané, ve všední den to znamená výpadek sběru — v obou
  // případech to uživatel musí vidět, tichá záměna dne je horší než demo.
  mockApiSDny([{ date: '2026-08-28', expiry: '20260828' }])
  renderApp()

  const banner = await screen.findByTestId('expiry-fallback-banner')
  expect(banner.textContent).toContain('2026-08-31')
  expect(banner.textContent).toContain('2026-08-28')
  expect(banner.textContent).toContain('sběr neběžel')
})

test('když data pro vybranou expiraci jsou, banner se neukáže (#946)', async () => {
  mockApiSDny([
    { date: '2026-08-28', expiry: '20260828' },
    { date: '2026-08-31', expiry: '20260831' },
  ])
  renderApp()

  await screen.findByLabelText('Heatmapa')
  expect(screen.queryByTestId('expiry-fallback-banner')).toBeNull()
})
