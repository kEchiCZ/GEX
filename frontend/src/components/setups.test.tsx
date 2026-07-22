/** Testy setup detektoru v UI (ADR-0004): obrazovka Setupy, hodnocení, WS refresh. */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { formatPct, formatPnlUsd, setupPnlPct, setupPnlUsd, setupRrr } from '../api/setups'
import { pointValue } from '../instrument/tick'
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

test('P/L setupu v USD na 1 kontrakt (#185)', () => {
  // riziko 29 bodů, outcome +0.48 R → 13.92 bodu × 50 $ (ES) = 696 $
  expect(setupPnlUsd({ entry: 7501, stop: 7472, outcome_r: 0.48 }, pointValue('ES'))).toBeCloseTo(
    696,
  )
  // plný stop = −1 R → −29 bodů × 20 $ (NQ) = −580 $
  expect(setupPnlUsd({ entry: 7501, stop: 7472, outcome_r: -1 }, pointValue('NQ'))).toBeCloseTo(
    -580,
  )
  expect(setupPnlUsd({ entry: 7501, stop: 7472, outcome_r: null }, 50)).toBeNull()
  expect(formatPnlUsd(696)).toBe('+696 $')
  expect(formatPnlUsd(-580.125)).toBe('-580.12 $') // Math.round půlí k +∞
})

test('P/L v % notional == procentní pohyb ceny (#189)', () => {
  // 0.48 R × 29 b = 13.92 b na entry 7501 → +0.1856 % (…× 50 $ / 375 050 $ dává totéž)
  expect(setupPnlPct({ entry: 7501, stop: 7472, outcome_r: 0.48 })).toBeCloseTo(0.18557, 4)
  expect(setupPnlPct({ entry: 7501, stop: 7472, outcome_r: null })).toBeNull()
  expect(setupPnlPct({ entry: 0, stop: 10, outcome_r: 1 })).toBeNull()
  expect(formatPct(0.18557)).toBe('+0.19 %')
  expect(formatPct(-1.5)).toBe('-1.50 %')
})

test('obrazovka Setupy: historie s výsledkem a hodnocením', async () => {
  const fetchMock = mockApi([SETUP_ROW])
  renderApp()

  fireEvent.click(screen.getByRole('button', { name: 'Setupy' }))
  expect(await screen.findByText('Neúspěšný průraz')).toBeDefined()
  expect(screen.getByText('+0.48')).toBeDefined()
  // 'Cíl' je hlavička sloupce i badge stavu — badge přidává druhý výskyt
  expect(screen.getAllByText('Cíl').length).toBe(2)
  // Čas uzavření a P/L v USD na 1 kontrakt (#185): 0.48 R × 29 b × 50 $ = 696 $
  const closedCell = document.querySelector('[data-part="closed-ts"]')
  expect(closedCell?.textContent).toMatch(/\d{1,2}:\d{2}/) // closed_ts se zobrazuje
  // P/L buňka nese dolary i % notional (#189)
  expect(document.querySelector('[data-part="pnl"]')?.textContent).toContain('+696 $')
  expect(document.querySelector('[data-part="pnl"]')?.textContent).toContain('+0.19 %')
  // Zvýrazněné souhrnné statistiky (#189)
  expect(screen.getByTestId('setups-total-pnl').textContent).toBe('+696 $')
  expect(screen.getByTestId('setups-total-pct').textContent).toBe('+0.19 %')
  expect(screen.getByText(/1 kontrakt/)).toBeDefined()

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
  // Čas vzniku setupu (created_ts) v kartě: datum + čas v lokální zóně (issue #113/#115)
  const cardTime = screen.getByLabelText('Aktivní setupy').querySelector('.setup-card-time')
  expect(cardTime?.textContent).toMatch(/\d{4}/) // rok = je tam datum
  expect(cardTime?.textContent).toMatch(/\d{1,2}:\d{2}/) // i čas

  fireEvent.click(screen.getByRole('button', { name: 'Skrýt setup 7' }))
  expect(screen.queryByLabelText('Aktivní setupy')).toBeNull()
})

test('alert ve zvonečku ukazuje čas notifikace (issue #113)', async () => {
  mockApi([])
  renderApp()
  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('alerts', {
      kind: 'setup',
      symbol: 'ES',
      message: 'Nový setup SHORT (failed_break): entry 29094, cíl 28780, stop 29109.8',
      ts: 1784301720,
    })
  })
  fireEvent.click(await screen.findByRole('button', { name: /Notifikace/ }))
  const time = document.querySelector('.alert-time')
  expect(time).not.toBeNull()
  expect(time!.textContent).toMatch(/\d{4}/) // datum
  expect(time!.textContent).toMatch(/\d{1,2}:\d{2}/) // čas
  // Globální zvoneček → u alertu i symbol instrumentu
  const dropdown = screen.getByRole('dialog', { name: 'Historie alertů' })
  expect(dropdown.textContent).toContain('ES')
})

test('setup alerty ve zvonečku proklikávají na graf / na Setupy (#186)', async () => {
  mockApi([])
  renderApp()
  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('alerts', {
      kind: 'setup',
      event: 'created',
      symbol: 'NQ',
      message: 'Nový setup LONG (wall_bounce): entry 23000, cíl 23060, stop 22970',
      ts: 1752822000,
    })
    ws.push('alerts', {
      kind: 'setup',
      event: 'closed',
      symbol: 'NQ',
      message: 'Setup #5 uzavřen: cíl zasažen, výsledek +2.00 R',
      ts: 1752823000,
    })
  })

  // Výsledek setupu → stránka Setupy daného instrumentu (vyhodnocení)
  fireEvent.click(screen.getByRole('button', { name: /Notifikace/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Otevřít vyhodnocení setupů NQ' }))
  expect(await screen.findByRole('heading', { name: 'Setupy — NQ' })).toBeDefined()

  // Nový setup → graf instrumentu (karta + entry/cíl/stop linie)
  fireEvent.click(screen.getByRole('button', { name: /Notifikace/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Otevřít graf NQ' }))
  expect(await screen.findByTestId('data-source')).toBeDefined()
  expect(screen.queryByRole('heading', { name: /Setupy —/ })).toBeNull()
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
