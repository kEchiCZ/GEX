/** Proklik z upozornění na zprávy (#1290): posun grafu se spotřebuje.

Heatmap se při odchodu z grafu (Dashboard, News…) odmontuje; po návratu nesmí
pohled znovu skočit na staré upozornění. Heatmap je tu nahrazená stubem, který
zaznamená `focusBucket` a hlásí aplikaci jako skutečná komponenta — test tak
hlídá kontrakt v App, ne kreslení. */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { tableFromArrays, tableToIPC } from 'apache-arrow'
import { useEffect } from 'react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'

type FocusBucket = { idx: number; nonce: number } | null

const heatmapStub = vi.hoisted(() => ({ mounts: [] as FocusBucket[][] }))

vi.mock('./Heatmap', () => ({
  Heatmap: function HeatmapStub({
    focusBucket = null,
    onFocusApplied,
  }: {
    focusBucket?: FocusBucket
    onFocusApplied?: (nonce: number) => void
  }) {
    useEffect(() => {
      heatmapStub.mounts.push([])
    }, [])
    useEffect(() => {
      heatmapStub.mounts.at(-1)?.push(focusBucket)
      if (focusBucket) onFocusApplied?.(focusBucket.nonce)
    }, [focusBucket, onFocusApplied])
    return <div aria-label="Heatmapa" />
  },
}))

const MINUTES = ['2026-07-16T15:00:00Z', '2026-07-16T15:01:00Z', '2026-07-16T15:02:00Z']

function replayBundle() {
  const strikes = [7595, 7600, 7605]
  const columns = {
    ts: [] as string[],
    strike: [] as number[],
    right: [] as string[],
    value: [] as number[],
  }
  for (const ts of MINUTES) {
    for (const strike of strikes) {
      for (const right of ['C', 'P']) {
        columns.ts.push(ts)
        columns.strike.push(strike)
        columns.right.push(right)
        columns.value.push(right === 'C' ? 0.5 : -0.5)
      }
    }
  }
  const table = tableFromArrays({
    ts_min: columns.ts,
    strike: Float64Array.from(columns.strike),
    right: columns.right,
    volume: Float64Array.from(columns.ts.map(() => 10)),
    oi: Float64Array.from(columns.ts.map(() => 1000)),
    delta: Float64Array.from(columns.value),
    stale_age: Float64Array.from(columns.ts.map(() => 0)),
  })
  return {
    symbol: 'ES',
    expiry: '20260716',
    date: '2026-07-16',
    snapshots_arrow_base64: btoa(String.fromCharCode(...tableToIPC(table, 'stream'))),
    levels: [],
    flow: [],
    bars: MINUTES.map((ts, index) => ({
      ts_min: ts,
      open: 7599,
      high: 7601,
      low: 7598,
      close: 7600 + index,
      volume: 500,
    })),
  }
}

const ALERT_ROW = {
  id: 11,
  ts_event: '2026-07-16T15:01:10+00:00',
  kind: 'headline',
  category: 'FED',
  importance: 3,
  title: 'Fed Waller: inflace se vrací',
  summary: null,
  sentiment_dir: -1,
  sentiment_score: -0.4,
  forecast: null,
  previous: null,
  actual: null,
  surprise_z: null,
}

beforeEach(() => {
  FakeWebSocket.reset()
  heatmapStub.mounts.length = 0
  localStorage.clear()
  const bundle = replayBundle()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      const target = String(url)
      if (target.includes('/replay/')) return { ok: true, json: async () => bundle }
      if (target.includes('/news/markers?ids=')) {
        return { ok: true, status: 200, json: async () => ({ news: [ALERT_ROW] }) }
      }
      if (target.includes('/news/markers')) {
        return { ok: true, status: 200, json: async () => ({ news: [] }) }
      }
      if (target.includes('/expiries')) {
        return { ok: true, json: async () => ({ expiries: ['20260716'] }) }
      }
      return { ok: false, status: 404, json: async () => ({}) }
    }),
  )
})

test('posun na upozornění proběhne jednou; po návratu z Dashboardu se nepřehraje', async () => {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(<App socket={socket} />)
  await waitFor(() => expect(screen.getByTestId('data-source').textContent).toContain('replay'))
  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('alerts', {
      kind: 'news_anomaly',
      symbol: 'ES',
      message: 'Reakce ES na zprávy z 17:01',
      ts: 1784214100,
      ts_event: ALERT_ROW.ts_event,
      event_ids: [ALERT_ROW.id],
    })
  })
  fireEvent.click(screen.getByRole('button', { name: /Notifikace/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Otevřít zprávy ES v grafu' }))
  // Koš minuty 15:01 = index 1 osy dne; aplikovaný požadavek se hned spotřebuje
  await waitFor(() => expect(heatmapStub.mounts.at(-1)).toContainEqual({ idx: 1, nonce: 1 }))
  await waitFor(() => expect(heatmapStub.mounts.at(-1)?.at(-1)).toBeNull())
  await screen.findByRole('dialog', { name: 'Zprávy v čase markeru' })
  fireEvent.click(screen.getByRole('button', { name: 'Zavřít zprávy' }))

  const nav = screen.getByRole('navigation', { name: 'Hlavní navigace' })
  fireEvent.click(within(nav).getByRole('button', { name: 'Dashboard' }))
  const mountsBefore = heatmapStub.mounts.length
  fireEvent.click(within(nav).getByRole('button', { name: 'Graf' }))
  await waitFor(() => expect(heatmapStub.mounts.length).toBe(mountsBefore + 1))
  // Nový mount grafu nedostane žádný požadavek na posun
  expect(heatmapStub.mounts.at(-1)?.every((focus) => focus === null)).toBe(true)
})
