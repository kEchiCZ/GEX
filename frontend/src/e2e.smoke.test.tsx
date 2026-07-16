/** E2E render smoke (issue #31): App nad /replay balíkem — engine→storage→API→frontend. */
import { render, screen, waitFor } from '@testing-library/react'
import { tableFromArrays, tableToIPC } from 'apache-arrow'
import { beforeEach, expect, test, vi } from 'vitest'
import App from './App'
import { LiveSocket } from './api/ws'
import { FakeWebSocket } from './test/fakeWs'

function buildReplayBundle() {
  const strikes = [7595, 7600, 7605]
  const minutes = ['2026-07-16T15:00:00Z', '2026-07-16T15:01:00Z', '2026-07-16T15:02:00Z']
  const tsColumn: string[] = []
  const strikeColumn: number[] = []
  const rightColumn: string[] = []
  const volumeColumn: number[] = []
  const oiColumn: number[] = []
  const deltaColumn: number[] = []
  minutes.forEach((ts, minuteIdx) => {
    strikes.forEach((strike) => {
      for (const right of ['C', 'P']) {
        tsColumn.push(ts)
        strikeColumn.push(strike)
        rightColumn.push(right)
        volumeColumn.push((minuteIdx + 1) * (right === 'C' ? 20 : 10))
        oiColumn.push(right === 'C' ? 1000 : 1400)
        deltaColumn.push(right === 'C' ? 0.5 : -0.5)
      }
    })
  })
  const table = tableFromArrays({
    ts_min: tsColumn,
    strike: Float64Array.from(strikeColumn),
    right: rightColumn,
    volume: Float64Array.from(volumeColumn),
    oi: Float64Array.from(oiColumn),
    delta: Float64Array.from(deltaColumn),
    stale_age: Float64Array.from(tsColumn.map(() => 0)),
  })
  return {
    symbol: 'ES',
    expiry: '20260716',
    date: '2026-07-16',
    snapshots_arrow_base64: btoa(String.fromCharCode(...tableToIPC(table, 'stream'))),
    levels: minutes.map((ts) => ({
      ts_min: ts,
      flip: 7604.5,
      call_wall: 7605,
      put_wall: 7595,
      centroid: 7600,
      total_gex: 25,
    })),
    flow: minutes.map((ts, i) => ({ ts_min: ts, flow_delta: i * 750, cum_delta: i * 750 })),
    bars: minutes.map((ts, i) => ({
      ts_min: ts,
      open: 7599,
      high: 7601,
      low: 7598,
      close: 7600 + i,
      volume: 500,
    })),
  }
}

beforeEach(() => FakeWebSocket.reset())

test('App vyrenderuje celý den z /replay balíku (heatmapa, profil, panely, playback)', async () => {
  const bundle = buildReplayBundle()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      const target = String(url)
      if (target.includes('/replay/')) return { ok: true, json: async () => bundle }
      if (target.includes('/expiries')) {
        return { ok: true, json: async () => ({ expiries: ['20260716'] }) }
      }
      if (target.includes('/annotations')) {
        return { ok: true, json: async () => ({ annotations: [] }) }
      }
      return { ok: false, status: 404, json: async () => ({}) }
    }),
  )
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(<App socket={socket} />)

  // Data source se přepne z demo na replay (jediný fetch balíku)
  await waitFor(() => expect(screen.getByTestId('data-source').textContent).toContain('replay'))

  // Heatmapa + profil + spodní panely + playback nad reálným balíkem
  expect(screen.getByLabelText('Heatmapa')).toBeDefined()
  expect(screen.getByTestId('profile-row-7600')).toBeDefined()
  expect(screen.getByLabelText('Vol panel')).toBeDefined()
  expect(screen.getByLabelText('Cum Δ panel')).toBeDefined()
  const slider = screen.getByLabelText('Pozice dne') as HTMLInputElement
  expect(slider.max).toBe('2') // 3 minuty dne
})
