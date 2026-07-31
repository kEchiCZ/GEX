/** Render test záložky Stats (#297, SPEC 9.6). */
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { StatsView } from './StatsView'
import { AppStateProvider } from '../state/AppState'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'

const WAVES = [
  {
    id: 1,
    symbol: 'ES',
    direction: 'RiskOff',
    start_date: '2026-07-20',
    end_date: '2026-07-24',
    depth: 0.8,
    length_days: 4,
  },
  {
    id: 2,
    symbol: 'ES',
    direction: 'RiskOff',
    start_date: '2026-07-26',
    end_date: null,
    depth: 1.58,
    length_days: 3,
  },
  {
    id: 3,
    symbol: 'ES',
    direction: 'RiskOn',
    start_date: '2026-07-10',
    end_date: '2026-07-18',
    depth: 1.2,
    length_days: 8,
  },
]

const STATS = [
  {
    regime: 'all',
    category: 'MACRO_INFLATION',
    importance: 3,
    surprise_bucket: 'neg_large',
    deferred: false,
    window_min: 5,
    symbol: 'ES',
    n: 34,
    ret_mean_bp: 8.0,
    hit_rate: 0.76,
    hit_rate_lb: 0.59,
  },
  {
    regime: 'all',
    category: 'FED',
    importance: 2,
    surprise_bucket: 'none',
    deferred: false,
    window_min: 5,
    symbol: 'ES',
    n: 12,
    ret_mean_bp: -1.0,
    hit_rate: 0.5,
    hit_rate_lb: 0.3,
  },
]

beforeEach(() => {
  FakeWebSocket.reset()
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      const body = url.includes('/setups/')
        ? { setups: [] }
        : url.includes('/stats/waves')
          ? { waves: WAVES }
          : url.includes('/news/stats')
            ? { stats: STATS }
            : url.includes('/settings')
              ? {
                  settings: {
                    retro_pass: {
                      ran_at: '2026-07-29T05:00:00+00:00',
                      classified: 12,
                      reactions: 96,
                      index_points: 480,
                    },
                  },
                }
              : {}
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) })
    }),
  )
})

function makeView() {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  return render(
    <AppStateProvider socket={socket}>
      <StatsView />
    </AppStateProvider>,
  )
}

test('zobrazí vlny, hit-raty s gate zvýrazněním a stav retro passu (SPEC 9.6)', async () => {
  makeView()

  // Vlny: aktuální vlna + průměr per směr + marker v histogramu
  await waitFor(() => expect(screen.getByText(/Aktuální vlna/)).toBeDefined())
  expect(screen.getByText(/od 2026-07-26, hloubka 1.58/)).toBeDefined()
  expect(screen.getByText(/Práh potvrzení/)).toBeDefined()
  expect(screen.getAllByTestId('wave-marker').length).toBeGreaterThan(0)
  expect(screen.getByLabelText('Histogram hloubek RiskOn')).toBeDefined()

  // Hit-raty: gate-open řádek zvýrazněný, mělký ne
  const inflationRow = screen.getByText('Inflace').closest('tr')!
  expect(inflationRow.className).toContain('stats-gate-open')
  const fedRow = screen.getByText('Fed').closest('tr')!
  expect(fedRow.className).not.toContain('stats-gate-open')

  // Retro pass ze settings (text je rozsekaný interpolacemi → přes sekci)
  expect(screen.getByLabelText('Retro pass').textContent).toContain('zpracováno 108 položek')
})
