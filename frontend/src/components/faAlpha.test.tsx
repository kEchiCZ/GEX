/** Badge kalibrované FA α / ρ ve stavové liště (#232 fáze 2, ADR-0057 bod 6, #1632, ADR-0061 #1666). */
import { act, render, screen } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { LiveSocket } from '../api/ws'
import { daysLabel, faAlphaBadge, type FaAlphaPayload } from '../hooks/useFaAlpha'
import { FakeWebSocket } from '../test/fakeWs'

function mockApi(
  alphas: Array<{ symbol: string; alpha: number; days: number; source?: string | null }>,
  minDays?: number,
) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      const target = String(url)
      if (target.includes('/fa/alpha')) {
        return { ok: true, json: async () => ({ alphas, min_days: minDays }) }
      }
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

test('badge ukáže kalibrovanou α aktivního symbolu s počtem dnů', async () => {
  mockApi([
    { symbol: 'ES', alpha: 0.34, days: 5 },
    { symbol: 'NQ', alpha: 0.48, days: 2 },
  ])
  renderApp()

  const badge = await screen.findByTestId('status-fa-alpha')
  expect(badge.textContent).toBe('FA α=0.34 · 5 dní')
  // Vysvětlení v tooltipu — uživatel musí vědět, co číslo znamená
  expect(badge.getAttribute('title')).toContain('flow-adjusted')
})

test('bez kalibračních bodů se badge nekreslí (engine jede na defaultu)', async () => {
  mockApi([])
  renderApp()

  await screen.findByTestId('status-greeks')
  expect(screen.queryByTestId('status-fa-alpha')).toBeNull()
})

test('pod v6 ukáže badge „FA ρ=… · n dní“ ze stavu ρ z objemu (#1666, ADR-0061)', async () => {
  mockApi([{ symbol: 'ES', alpha: 0.27, days: 16, source: 'volume' }], 5)
  renderApp()
  act(() => {
    const ws = FakeWebSocket.latest()
    ws.open()
    ws.push('status', {
      engine: 'online',
      cumdelta_source: 'aggressor',
      cumdelta_coverage: { ES: { printed_share: null, printed_share_total: null } },
    })
  })

  const badge = await screen.findByTestId('status-fa-alpha')
  await vi.waitFor(() => expect(badge.textContent).toBe('FA ρ=0.27 · 16 dní'))
  const title = badge.getAttribute('title') ?? ''
  expect(title).toContain('\n• ')
  expect(title).toContain('ρ·objem strany')
  expect(title).toContain('zavírání pozic nevidí')
})

test('pod tisky (v6) ukáže badge „nekalibrováno n/5“ ze stavu ρ (#1632, #1666)', async () => {
  mockApi([{ symbol: 'ES', alpha: 0.12, days: 2, source: 'volume' }], 5)
  renderApp()
  act(() => {
    const ws = FakeWebSocket.latest()
    ws.open()
    ws.push('status', {
      engine: 'online',
      cumdelta_source: 'aggressor',
      cumdelta_coverage: { ES: { printed_share: null, printed_share_total: null } },
    })
  })

  const badge = await screen.findByTestId('status-fa-alpha')
  await vi.waitFor(() => expect(badge.textContent).toBe('FA nekalibrováno 2/5'))
  // Tooltip po odrážkách (vzor ivRankTooltip), ne odstavec
  expect(badge.getAttribute('title')).toContain('\n• ')
  expect(badge.getAttribute('title')).toContain('2 z 5 platných dnů')
})

const PAYLOAD: FaAlphaPayload = {
  minDays: 5,
  bySymbol: {
    ES: { alpha: 0.34, days: 12, source: 'midpoint' },
    NQ: { alpha: 0.2, days: 5, source: 'volume' },
    RTY: { alpha: 0.3, days: 4, source: 'volume' },
    YM: { alpha: 0.25, days: 9, source: 'aggressor' },
  },
}

test('faAlphaBadge: pod v5 beze změny, pod tisky jen ρ z objemu s ≥ min_days (#1632, #1666)', () => {
  // v5 (midpoint, i neznámý zdroj = starší engine): uložená α, bez stavu nic
  expect(faAlphaBadge(PAYLOAD, 'ES', 'midpoint', [])).toEqual({
    kind: 'calibrated',
    alpha: 0.34,
    days: 12,
    source: 'midpoint',
  })
  expect(faAlphaBadge(PAYLOAD, 'ES', undefined, [])?.kind).toBe('calibrated')
  expect(faAlphaBadge(PAYLOAD, 'GC', 'midpoint', ['GC'])).toBeNull()
  // v6: stav z midpointu je jiná veličina → 0 dnů
  expect(faAlphaBadge(PAYLOAD, 'ES', 'aggressor', ['ES'])).toEqual({
    kind: 'uncalibrated',
    days: 0,
    minDays: 5,
  })
  expect(faAlphaBadge(PAYLOAD, 'NQ', 'aggressor', ['NQ'])).toEqual({
    kind: 'calibrated',
    alpha: 0.2,
    days: 5,
    source: 'volume',
  })
  expect(faAlphaBadge(PAYLOAD, 'RTY', 'aggressor', ['RTY'])).toEqual({
    kind: 'uncalibrated',
    days: 4,
    minDays: 5,
  })
  // α z netu tisků (před ADR-0061) engine pod v6 nebere (`live_alpha`) → 0 dnů
  expect(faAlphaBadge(PAYLOAD, 'YM', 'aggressor', ['YM'])).toEqual({
    kind: 'uncalibrated',
    days: 0,
    minDays: 5,
  })
  // Bez stavu „0/5“, ne default α — ale jen u symbolu s CumΔ trackerem
  expect(faAlphaBadge(PAYLOAD, 'GC', 'aggressor', ['GC'])).toEqual({
    kind: 'uncalibrated',
    days: 0,
    minDays: 5,
  })
  expect(faAlphaBadge(PAYLOAD, 'SPY', 'aggressor', ['ES'])).toBeNull()
  // API nedostupné → žádný badge
  expect(faAlphaBadge(null, 'ES', 'aggressor', ['ES'])).toBeNull()
  // Starší API bez min_days: pod tisky práh nehádat, pod v5 beze změny
  const noMin = { ...PAYLOAD, minDays: null }
  expect(faAlphaBadge(noMin, 'NQ', 'aggressor', ['NQ'])).toBeNull()
  expect(faAlphaBadge(noMin, 'ES', 'midpoint', ['ES'])?.kind).toBe('calibrated')
  // Stav ρ pod v5 (návrat pinem image z v6): FA jede s uloženým faktorem jako live_alpha
  expect(faAlphaBadge(PAYLOAD, 'RTY', 'midpoint', ['RTY'])).toEqual({
    kind: 'calibrated',
    alpha: 0.3,
    days: 4,
    source: 'volume',
  })
})

test('daysLabel skloňuje česky', () => {
  expect(daysLabel(1)).toBe('1 den')
  expect(daysLabel(3)).toBe('3 dny')
  expect(daysLabel(5)).toBe('5 dní')
})
