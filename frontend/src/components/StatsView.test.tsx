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

const GROUP = {
  count: 3,
  active: 0,
  closed: 3,
  wins: 2,
  losses: 1,
  win_rate: 2 / 3,
  sum_r: 2.5,
  avg_r: 2.5 / 3,
  gross_usd: 1250,
  fees_usd: 30,
  net_usd: 1220,
  ev_r: { ev: 2.5 / 3, win_rate: 2 / 3, loss_rate: 1 / 3, avg_win: 1.75, avg_loss: 1, n: 3 },
  ev_usd: null,
}

/** Serverový souhrn setupů (#1319) — Stats ho jen vykresluje. */
const SUMMARY = {
  symbols: ['ES'],
  mechanics_version: 5,
  all_versions: false,
  total_count: 603,
  legacy_count: 290,
  after_settle_count: 0,
  fee_per_contract_usd: 10,
  account_usd: 50000,
  unpriced_symbols: [],
  all: GROUP,
  tradeable: { ...GROUP, count: 0, closed: 0 },
  shadow: { ...GROUP, count: 2 },
  unruled: GROUP,
  shadow_reasons: { gate: 2 },
  account: null,
  today: null,
  band_gates: null,
  regimes: [{ template: 'wall_bounce', regime: 'negative', n: 4, wins: 3, win_rate: 0.75 }],
  performance: {
    daily: [
      { session: '2026-09-24', trades: 2, sum_r: 3, cum_r: 3 },
      { session: '2026-09-25', trades: 1, sum_r: -0.5, cum_r: 2.5 },
    ],
    sharpe_all: { sharpe: 11.2, days: 2 },
    sharpe_30: { sharpe: 11.2, days: 2 },
    max_drawdown_r: -0.5,
    simulation: null,
  },
}

beforeEach(() => {
  FakeWebSocket.reset()
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      const body = url.includes('/setups/summary')
        ? SUMMARY
        : url.includes('/setups/')
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

test('Výkon a režimy setupů vykreslí serverový souhrn celé historie (#1319)', async () => {
  makeView()
  expect((await screen.findByTestId('stats-balance')).textContent).toContain('+2.5 R')
  expect(screen.getByTestId('stats-balance').textContent).toContain('3 obchodů')
  expect(screen.getByTestId('stats-one-contract').textContent).toContain('+1220 $')
  expect(screen.getByTestId('stats-ev').textContent).toContain('n=3')
  const regimes = screen.getByLabelText('Setupy per režim')
  expect(regimes.textContent).toContain('Odraz od zdi')
  expect(regimes.textContent).toContain('75 %')
  expect(regimes.textContent).toContain('(v5)')
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

  // Souhrn setupů ze serveru (#1319): /setups/summary?symbols=…
  await waitFor(() => expect(setupCalls().some((url) => url.includes('symbols=ES'))).toBe(true))
  expect(setupCalls().some((url) => url.includes('symbols=NQ'))).toBe(false)

  fireEvent.click(screen.getByText('Přepnout na NQ'))
  await waitFor(() => expect(setupCalls().some((url) => url.includes('symbols=NQ'))).toBe(true))
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
