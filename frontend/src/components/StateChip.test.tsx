/** Test chipu stavu sentimentu: badge korekční epizody a řádek prahu v popoveru (#565). */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { StateChip } from './StateChip'
import { AppStateProvider } from '../state/AppState'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'

const STATE = {
  symbol: 'ES',
  state: 'RiskOff',
  polarity: 'down',
  unconfirmed: false,
  unconfirmed_state: 'RiskOff',
  last_close: -2.87,
  sigma: 2.61,
  ma5: -3.2,
  ma10: -2.69,
  threshold: 0,
  current_wave: null,
  episode_status: 'open',
  episode: {
    start_date: '2026-09-13',
    end_date: null,
    ref_level_z: 0.11,
    depth_z: 1.32,
    label: null,
    length_days: 1,
  },
  last_episode: null,
  correction_threshold: -0.89,
  correction_threshold_d: 1,
  episode_horizon_h: 10,
  episode_params_version: 1,
}

const EPISODES = [
  { id: 1, symbol: 'ES', start_date: '2026-08-05', end_date: '2026-08-19', ref_level_z: 3.86, depth_z: 6.78, label: 'negation', length_days: 10, params_version: 1, series_variant: 'zscore_100' }, // prettier-ignore
  { id: 2, symbol: 'ES', start_date: '2026-09-13', end_date: null, ref_level_z: 0.11, depth_z: 1.32, label: null, length_days: 1, params_version: 1, series_variant: 'zscore_100' }, // prettier-ignore
]

beforeEach(() => {
  FakeWebSocket.reset()
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      const body = url.includes('/sentiment/state')
        ? STATE
        : url.includes('/stats/episodes')
          ? { episodes: EPISODES }
          : url.includes('/sentiment/index/')
            ? { series: [] }
            : url.includes('/sentiment/topics')
              ? { topics: [] }
              : { expiries: [] }
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) })
    }),
  )
})

test('badge KOREKCE n d s tooltipem předběžnosti; popover nese práh v σ (#565)', async () => {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(
    <AppStateProvider socket={socket}>
      <StateChip />
    </AppStateProvider>,
  )
  const badge = await screen.findByTestId('state-episode')
  expect(badge.textContent).toBe('KOREKCE 1 d')
  expect(badge.className).toContain('state-episode-open')
  expect(badge.getAttribute('title')).toContain('Probíhá od 2026-09-13')
  expect(badge.getAttribute('title')).toContain('placeholder z prvního měření')

  fireEvent.click(screen.getByTestId('state-chip'))
  const row = await screen.findByTestId('state-episode-row')
  expect(row.textContent).toContain('práh -0.89 σ')
  expect(row.textContent).toContain('korekce 1 d 1.32 σ')
  // Po otevření se dopočte počet rozhodnutých epizod do tooltipu (1 negace)
  await screen.findByText(/korekce: předběžné \(v1/)
  expect((await screen.findByTestId('state-episode')).getAttribute('title')).toContain(
    'rozhodnutých epizod: 1',
  )
})
