/** FA zdroj OI při mezeře (ADR-0057 bod 4, #1632): minuta bez FA řad je
mezera, ne tiché měřené OI; den bez FA řad to badge přizná.

Heatmap je nahrazená stubem, který si pamatuje poslední `grid` — test hlídá
kontrakt v App (co jde do grafu), ne kreslení. */
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { tableFromArrays, tableToIPC } from 'apache-arrow'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { LiveSocket } from '../api/ws'
import type { HeatmapGrid } from '../heatmap/grid'
import { clearBundleCache } from '../replay/bundleCache'
import { FakeWebSocket } from '../test/fakeWs'

const heatmapStub = vi.hoisted(() => ({ grid: null as HeatmapGrid | null }))

vi.mock('./Heatmap', () => ({
  Heatmap: function HeatmapStub({ grid }: { grid: HeatmapGrid }) {
    heatmapStub.grid = grid
    return <div aria-label="Heatmapa" />
  },
}))

const MINUTES = ['2026-07-16T15:00:00Z', '2026-07-16T15:01:00Z', '2026-07-16T15:02:00Z']

function replayBundle(faMinutes: string[]) {
  const ts: string[] = []
  const right: string[] = []
  for (const minute of MINUTES) {
    for (const side of ['C', 'P']) {
      ts.push(minute)
      right.push(side)
    }
  }
  const table = tableFromArrays({
    ts_min: ts,
    strike: Float64Array.from(ts.map(() => 7600)),
    right,
    volume: Float64Array.from(ts.map(() => 10)),
    oi: Float64Array.from(ts.map(() => 1000)),
    delta: Float64Array.from(right.map((side) => (side === 'C' ? 0.5 : -0.5))),
    stale_age: Float64Array.from(ts.map(() => 0)),
  })
  return {
    symbol: 'ES',
    expiry: '20260716',
    date: '2026-07-16',
    snapshots_arrow_base64: btoa(String.fromCharCode(...tableToIPC(table, 'stream'))),
    levels: [],
    flow: [],
    levelsfa: faMinutes.map((minute) => ({ ts_min: minute, flip: 7590 })),
    bars: MINUTES.map((minute, index) => ({
      ts_min: minute,
      open: 7599,
      high: 7601,
      low: 7598,
      close: 7600 + index,
      volume: 500,
    })),
  }
}

function mockApi(faMinutes: string[]) {
  const bundle = replayBundle(faMinutes)
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      const target = String(url)
      if (target.includes('/replay/')) return { ok: true, json: async () => bundle }
      if (target.includes('/expiries')) {
        return { ok: true, json: async () => ({ expiries: ['20260716'] }) }
      }
      return { ok: false, status: 404, json: async () => ({}) }
    }),
  )
}

async function renderWithFa() {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(<App socket={socket} />)
  await waitFor(() => expect(screen.getByTestId('data-source').textContent).toContain('replay'))
  fireEvent.change(await screen.findByLabelText('Zdroj OI'), { target: { value: 'fa' } })
  return screen.findByTestId('fa-badge')
}

/** Součet vrstvy call ve sloupci minuty (grid je kompaktní: strikeIdx · minutes + minuteIdx). */
function callColumn(minuteIdx: number): number {
  const grid = heatmapStub.grid!
  let total = 0
  for (let strikeIdx = 0; strikeIdx < grid.strikes.length; strikeIdx += 1) {
    total += grid.layers.call?.[strikeIdx * grid.minutes + minuteIdx] ?? 0
  }
  return total
}

beforeEach(() => {
  FakeWebSocket.reset()
  heatmapStub.grid = null
  localStorage.clear()
  clearBundleCache() // balík dne se cachuje per symbol × expirace × den
})

test('minuta bez FA řad je v režimu FA prázdná, ne měřené OI', async () => {
  mockApi([MINUTES[0], MINUTES[2]])
  const badge = await renderWithFa()
  expect(badge.textContent).toBe('FA odhad')
  await waitFor(() => expect(callColumn(1)).toBe(0))
  expect(callColumn(0)).toBeGreaterThan(0)
  expect(callColumn(2)).toBeGreaterThan(0)
})

test('den bez FA řad: badge to přizná a OI vrstvy jsou prázdné', async () => {
  mockApi([])
  const badge = await renderWithFa()
  expect(badge.textContent).toBe('FA odhad · den bez FA')
  expect(badge.getAttribute('title')).toContain('tento den FA řady nemá')
  await waitFor(() => expect([0, 1, 2].map(callColumn)).toEqual([0, 0, 0]))
})
