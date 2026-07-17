/** Testy přepínání timeframů a watchlistu: agregace mění délku dne, Daily fetch, tickery. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'
import { INTERVALS, defaultExpiry } from '../state/AppState'

function mockApi() {
  const fetchMock = vi.fn(async (url: unknown, init?: RequestInit) => {
    const target = String(url)
    if (target.includes('/expiries')) {
      return { ok: true, json: async () => ({ expiries: ['20260716'] }) }
    }
    if (init?.method === 'POST' && target.includes('/watchlist')) {
      const body = JSON.parse(String(init.body)) as { symbol: string }
      return { ok: true, json: async () => ({ id: 3, symbol: body.symbol }) }
    }
    if (target.includes('/watchlist')) {
      return {
        ok: true,
        json: async () => ({
          watchlist: [
            { id: 1, symbol: 'ES' },
            { id: 2, symbol: 'NQ' },
          ],
        }),
      }
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

test('defaultExpiry: dnešní expirace má přednost, jinak nejnovější', () => {
  const today = new Date().toISOString().slice(0, 10).replaceAll('-', '')
  expect(defaultExpiry(['20250101', today, '20991231'])).toBe(today)
  expect(defaultExpiry(['20250101', '20250102'])).toBe('20250102')
  expect(defaultExpiry([])).toBeNull()
})

test('timeframe řádek nabízí celou sadu 1m–1d', () => {
  mockApi()
  renderApp()
  for (const interval of INTERVALS) {
    expect(screen.getByRole('button', { name: interval })).toBeDefined()
  }
})

test('přepnutí timeframe agreguje den — mění se rozsah playbacku', () => {
  mockApi()
  renderApp()
  fireEvent.click(screen.getByLabelText('Replay ovládání')) // lišta je defaultně skrytá
  const slider = () => screen.getByLabelText('Pozice dne') as HTMLInputElement
  // Demo den má 390 minut → 1m: lastIndex 389
  expect(slider().max).toBe('389')
  fireEvent.click(screen.getByRole('button', { name: '5m' }))
  expect(slider().max).toBe('77') // ceil(390/5)=78 košů
  fireEvent.click(screen.getByRole('button', { name: '1h' }))
  expect(slider().max).toBe('6') // ceil(390/60)=7 košů
  fireEvent.click(screen.getByRole('button', { name: '1m' }))
  expect(slider().max).toBe('389')
  // Live pozice zůstává live — přepnutí tam a zpět nesmí „ztratit den"
  expect(slider().value).toBe('389')
})

test('playback: ne-live pozice se při změně timeframe mapuje proporcionálně', () => {
  mockApi()
  renderApp()
  fireEvent.click(screen.getByLabelText('Replay ovládání'))
  const slider = () => screen.getByLabelText('Pozice dne') as HTMLInputElement
  fireEvent.change(slider(), { target: { value: '195' } }) // polovina dne na 1m
  fireEvent.click(screen.getByRole('button', { name: '1h' }))
  expect(slider().value).toBe('3') // ~polovina ze 7 košů
  fireEvent.click(screen.getByRole('button', { name: '1m' }))
  expect(Number(slider().value)).toBeGreaterThan(150) // zpět ~doprostřed, ne na začátek
})

test('replay lišta je defaultně skrytá (vždy live) a zavření vrací na live', () => {
  mockApi()
  renderApp()
  expect(screen.queryByLabelText('Pozice dne')).toBeNull()
  const toggle = screen.getByLabelText('Replay ovládání')
  fireEvent.click(toggle)
  const slider = screen.getByLabelText('Pozice dne') as HTMLInputElement
  fireEvent.change(slider, { target: { value: '100' } }) // přetočení do minulosti
  expect(slider.value).toBe('100')
  fireEvent.click(toggle) // zavření → skrytí + návrat na live
  expect(screen.queryByLabelText('Pozice dne')).toBeNull()
  fireEvent.click(toggle)
  expect((screen.getByLabelText('Pozice dne') as HTMLInputElement).value).toBe('389')
})

test('demo zdroj dat ukazuje zřetelný banner', () => {
  mockApi()
  renderApp()
  expect(screen.getByText(/Demo data — pro ES/)).toBeDefined()
})

test('Daily režim: stáhne seznam dnů a zakáže intraday koše', async () => {
  const fetchMock = mockApi()
  renderApp()
  fireEvent.click(screen.getByRole('button', { name: 'Daily' }))

  await waitFor(() => {
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/instruments/ES/days'))).toBe(
      true,
    )
  })
  expect((screen.getByRole('button', { name: '5m' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: 'Intraday' }))
  expect((screen.getByRole('button', { name: '5m' }) as HTMLButtonElement).disabled).toBe(false)
})

test('Mode/Scale selecty jsou nad demo daty zakázané; Walls select přepíná bez pádu', () => {
  mockApi()
  renderApp()
  // Demo data nemají surovou matici → módy/škály nejde přepínat
  expect((screen.getByLabelText('Heatmap mód') as HTMLSelectElement).disabled).toBe(true)
  expect((screen.getByLabelText('Škála heatmapy') as HTMLSelectElement).disabled).toBe(true)
  const walls = screen.getByLabelText('Walls mód') as HTMLSelectElement
  expect(walls.disabled).toBe(false)
  fireEvent.change(walls, { target: { value: 'peak' } })
  fireEvent.change(walls, { target: { value: 'ridge' } })
  fireEvent.change(walls, { target: { value: 'off' } })
})

test('hlavička ukazuje poslední cenu a denní změnu z dat dne', () => {
  mockApi()
  renderApp()
  const last = document.querySelector('.instrument-price .last')
  expect(last?.textContent).not.toBe('—')
  expect(
    document.querySelector('.instrument-price .change-up, .instrument-price .change-down'),
  ).not.toBeNull()
})

test('předěl mezi grafem a pravým panelem mění šířku profilu tažením', () => {
  mockApi()
  renderApp()
  const divider = screen.getByRole('separator', { name: 'Šířka pravého panelu' })
  const profileSvg = () =>
    screen.getByRole('img', { name: 'Skládané pruhy strike profilu' }) as unknown as SVGSVGElement
  expect(profileSvg().getAttribute('width')).toBe('260')

  fireEvent.pointerDown(divider, { clientX: 1000, pointerId: 1 })
  fireEvent.pointerMove(divider, { clientX: 900, pointerId: 1 }) // tažení doleva → širší panel
  fireEvent.pointerUp(divider, { pointerId: 1 })
  expect(profileSvg().getAttribute('width')).toBe('360')

  // Meze: nejde zmenšit pod 180
  fireEvent.pointerDown(divider, { clientX: 500, pointerId: 1 })
  fireEvent.pointerMove(divider, { clientX: 2000, pointerId: 1 })
  fireEvent.pointerUp(divider, { pointerId: 1 })
  expect(profileSvg().getAttribute('width')).toBe('180')
})

test('watchlist: kliknutí přepne ticker, přidání volá POST', async () => {
  const fetchMock = mockApi()
  renderApp()

  // Watchlist z API: ES + NQ; kliknutí na NQ přepne aktivní symbol (hlavička)
  const switchNq = await screen.findByRole('button', { name: 'Přepnout na NQ' })
  fireEvent.click(switchNq)
  await waitFor(() => {
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).endsWith('/instruments/NQ/expiries')),
    ).toBe(true)
  })

  // Přidání nového tickeru → POST /watchlist (symbol se normalizuje na velká písmena)
  fireEvent.change(screen.getByLabelText('Nový symbol'), { target: { value: 'cl' } })
  fireEvent.click(screen.getByLabelText('Přidat do watchlistu'))
  await waitFor(() => {
    expect(screen.getByRole('button', { name: 'Přepnout na CL' })).toBeDefined()
  })
})
