/** Přepínač zdroje OI (#232 fáze 2): default měřené, persistence per symbol. */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'

function mockApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      const target = String(url)
      if (target.includes('/expiries')) {
        return { ok: true, json: async () => ({ expiries: ['20260716'] }) }
      }
      return { ok: false, status: 404, json: async () => ({}) }
    }),
  )
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
  window.localStorage.clear()
})

test('default je měřené; volba FA se uloží per symbol', async () => {
  mockApi()
  renderApp()

  const select = (await screen.findByLabelText('Zdroj OI')) as HTMLSelectElement
  expect(select.value).toBe('measured')

  fireEvent.change(select, { target: { value: 'fa' } })
  expect(select.value).toBe('fa')
  const stored = JSON.parse(window.localStorage.getItem('gexlens.oiSourceBySymbol') ?? '{}')
  expect(stored.ES).toBe('fa')

  // Bez FA dat (demo den nemá řadu oiest) se badge nekreslí — graf nesmí
  // tvrdit odhad, který nemá; kreslí se dál měřená data
  expect(screen.queryByTestId('fa-badge')).toBeNull()
})

test('uložená volba per symbol se po refreshi obnoví (#232)', async () => {
  window.localStorage.setItem(
    'gexlens.oiSourceBySymbol',
    JSON.stringify({ ES: 'fa', NQ: 'measured' }),
  )
  window.localStorage.setItem('gexlens.symbol', JSON.stringify('ES'))
  mockApi()
  renderApp()

  const select = (await screen.findByLabelText('Zdroj OI')) as HTMLSelectElement
  expect(select.value).toBe('fa')
  // Aktivní FA volba je vizuálně odlišená (tečkovaný okraj chipu)
  expect(select.className).toContain('fa-source-active')
})
