/** Testy checkboxu Setupy (#399): globální viditelnost vrstvy setupů v grafu. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { CURRENT_MECHANICS_VERSION } from '../api/setups'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'

// Heatmapa kreslí linie na canvas (v jsdom neběží) — mock vypíše jména levels
// linií do DOM, ať jde ověřit, že checkbox skryje i entry/cíl/stop linie
vi.mock('./Heatmap', () => ({
  Heatmap: ({ overlays }: { overlays?: { levels?: Array<{ name: string }> } }) => (
    <div data-testid="heatmap-level-names">
      {(overlays?.levels ?? []).map((line) => line.name).join(' ')}
    </div>
  ),
}))

const ACTIVE_SETUP = {
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
  status: 'active',
  closed_ts: null,
  outcome_r: null,
  mfe: null,
  mae: null,
  user_rating: null,
  user_note: null,
  mechanics_version: CURRENT_MECHANICS_VERSION,
}

function mockApi(setups: Array<Record<string, unknown>>) {
  const fetchMock = vi.fn(async (url: unknown) => {
    const target = String(url)
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

const levelNames = () => screen.getByTestId('heatmap-level-names').textContent ?? ''

beforeEach(() => {
  FakeWebSocket.reset()
  vi.restoreAllMocks()
})

test('checkbox Setupy skryje kartu i entry/cíl/stop linie (#399)', async () => {
  mockApi([ACTIVE_SETUP])
  renderApp()

  // Default zapnuto (dnešní chování): karta nad grafem i linie v heatmapě
  expect(await screen.findByLabelText('Aktivní setupy')).toBeDefined()
  await waitFor(() => expect(levelNames()).toContain('setup-entry-7'))
  expect(levelNames()).toContain('setup-target-7')
  expect(levelNames()).toContain('setup-stop-7')

  // Vypnutí skryje celou vrstvu — kartu i linie
  fireEvent.click(screen.getByRole('checkbox', { name: 'Setupy' }))
  expect(screen.queryByLabelText('Aktivní setupy')).toBeNull()
  expect(levelNames()).not.toContain('setup-entry-7')
  expect(levelNames()).not.toContain('setup-target-7')
  expect(levelNames()).not.toContain('setup-stop-7')
  // Volba se persistuje jako ostatní přepínače (ADR-0007)
  await waitFor(() => {
    const stored = JSON.parse(window.localStorage.getItem('gexlens.toggles') ?? '{}') as {
      setups?: boolean
    }
    expect(stored.setups).toBe(false)
  })

  // Zapnutí vrstvu vrátí
  fireEvent.click(screen.getByRole('checkbox', { name: 'Setupy' }))
  expect(await screen.findByLabelText('Aktivní setupy')).toBeDefined()
  expect(levelNames()).toContain('setup-entry-7')
})

test('stránka Setupy funguje i s vypnutým checkboxem (#399)', async () => {
  mockApi([ACTIVE_SETUP])
  renderApp()

  // Checkbox řídí jen vrstvu v grafu — vyhodnocení setupů zůstává dostupné
  fireEvent.click(await screen.findByRole('checkbox', { name: 'Setupy' }))
  fireEvent.click(screen.getByRole('button', { name: 'Setupy' }))
  expect(await screen.findByRole('heading', { name: 'Setupy — ES' })).toBeDefined()
  expect(await screen.findByText(/Neúspěšný průraz/)).toBeDefined()
})
