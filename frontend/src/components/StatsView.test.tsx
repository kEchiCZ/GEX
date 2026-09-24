/** Render test záložky Stats (#297, SPEC 9.6). */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { StatsView } from './StatsView'
import { AppStateProvider, useAppState } from '../state/AppState'
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
    gate_open: true,
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
    gate_open: false,
  },
]

// Korekční epizody (#565): jedna negace, jeden pokus, jedna probíhající
const EPISODES = [
  {
    id: 1,
    symbol: 'ES',
    start_date: '2026-08-05',
    end_date: '2026-08-19',
    ref_level_z: 3.86,
    depth_z: 6.78,
    label: 'negation',
    length_days: 10,
    params_version: 1,
    series_variant: 'zscore_100',
  },
  {
    id: 2,
    symbol: 'ES',
    start_date: '2026-08-28',
    end_date: '2026-09-11',
    ref_level_z: 0.01,
    depth_z: 3.41,
    label: 'attempt',
    length_days: 10,
    params_version: 1,
    series_variant: 'zscore_100',
  },
  {
    id: 3,
    symbol: 'ES',
    start_date: '2026-09-13',
    end_date: null,
    ref_level_z: 0.11,
    depth_z: 1.32,
    label: null,
    length_days: 1,
    params_version: 1,
    series_variant: 'zscore_100',
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
        : url.includes('/stats/episodes')
          ? { episodes: EPISODES }
          : url.includes('/stats/waves')
            ? { waves: WAVES }
            : url.includes('/news/stats')
              ? { stats: STATS, gate: { min_samples: 30, wilson_lb: 0.5, min_effect_bp: 1 } }
              : url.includes('/briefing/verdicts/stats')
                ? {
                    evaluated: 3,
                    min_samples: 30,
                    by_verdict: { long: { n: 3, hits: 2, unscored: 0, hit_rate: 0.667, wilson_lb: 0.208, gate_open: false } }, // prettier-ignore
                    by_vote: { trend_higher: { n: 3, hits: 3, hit_rate: 1, wilson_lb: 0.439, gate_open: false } }, // prettier-ignore
                  }
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

/** Tlačítko na přepnutí symbolu — simulace sidebaru pro test #500. */
function SymbolSwitch({ to }: { to: string }) {
  const { setSymbol } = useAppState()
  return (
    <button type="button" onClick={() => setSymbol(to)}>
      Přepnout na {to}
    </button>
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

  // Korekční epizody (#565): karty per třída, tabulka, badge předběžné (2 rozhodnuté < 20)
  const episodesSection = screen.getByLabelText('Korekční epizody')
  expect(episodesSection.textContent).toContain('1 epizod · hloubka Ø 3.41 σ')
  expect(episodesSection.textContent).toContain('1 epizod · hloubka Ø 6.78 σ')
  expect(episodesSection.textContent).toContain('Probíhá: 1')
  expect(screen.getByTestId('episodes-preliminary').textContent).toContain('2 rozhodnutých')
  const rows = screen.getByTestId('episodes-table').querySelectorAll('tbody tr')
  expect(rows).toHaveLength(3)
  expect(rows[0].textContent).toContain('2026-09-13') // nejnovější první
  expect(rows[0].textContent).toContain('probíhá')
})

test('přepnutí symbolu refetchne tabulku setupů s novým symbolem (#500)', async () => {
  window.localStorage.clear()
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(
    <AppStateProvider socket={socket}>
      <SymbolSwitch to="NQ" />
      <StatsView />
    </AppStateProvider>,
  )
  const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>
  const setupCalls = () =>
    fetchMock.mock.calls.map((call) => String(call[0])).filter((url) => url.includes('/setups/'))

  await waitFor(() => expect(setupCalls().some((url) => url.includes('/setups/ES'))).toBe(true))
  expect(setupCalls().some((url) => url.includes('/setups/NQ'))).toBe(false)

  fireEvent.click(screen.getByText('Přepnout na NQ'))
  await waitFor(() => expect(setupCalls().some((url) => url.includes('/setups/NQ'))).toBe(true))
})

test('sekce Verdikt dne (#1091): tabulky per verdikt a složku, brána sběr', async () => {
  makeView()
  await waitFor(() => expect(screen.getByTestId('verdict-stats')).toBeTruthy())
  const text = screen.getByTestId('verdict-stats').textContent ?? ''
  expect(text).toContain('spíše long')
  expect(text).toContain('67 %')
  expect(text).toContain('sběr (3/30)')
  expect(text).toContain('trend_higher')
})
