/** Persistence pohledu na konci gesta v celé aplikaci (#1419, E-2a.6): jedno
gesto kolečka nebo panu = jeden zápis `gexlens.chartZoomX` a `gexlens.chartYRange`
do localStorage (dřív zápis na každý pointermove i krok kolečka). */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'
import { WHEEL_IDLE_MS } from '../heatmap/view'

const CENTER = { clientX: 600, clientY: 300 }

let frames = new Map<number, FrameRequestCallback>()
let nextFrame = 1

function runFrame() {
  act(() => {
    const pending = [...frames.values()]
    frames = new Map()
    for (const callback of pending) callback(performance.now())
  })
}

function mockApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      const target = String(url)
      if (target.includes('/expiries')) {
        return { ok: true, json: async () => ({ expiries: ['20260717'] }) }
      }
      if (target.includes('/watchlist')) {
        return { ok: true, json: async () => ({ watchlist: [{ id: 1, symbol: 'ES' }] }) }
      }
      return { ok: false, status: 404, json: async () => ({}) }
    }),
  )
}

beforeEach(() => {
  FakeWebSocket.reset()
  frames = new Map()
  nextFrame = 1
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    const id = nextFrame++
    frames.set(id, callback)
    return id
  })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => {
    frames.delete(id)
  })
  mockApi()
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

test('jedno gesto = jeden zápis zoomu X a pásma Y do localStorage', async () => {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(<App socket={socket} />)
  const overlay = await screen.findByRole('img', { name: 'GEX heatmapa' })
  runFrame()
  const setItem = vi.spyOn(Storage.prototype, 'setItem')
  const writes = (name: string) =>
    setItem.mock.calls.filter(([key]) => key === `gexlens.${name}`).length

  // Kolečko: 5 kroků, zápis až po klidu
  for (let step = 0; step < 5; step += 1) {
    fireEvent.wheel(overlay, { deltaY: -100, ...CENTER })
    runFrame()
  }
  expect(writes('chartZoomX')).toBe(0)
  expect(writes('chartYRange')).toBe(0)
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, WHEEL_IDLE_MS + 50))
  })
  expect(writes('chartZoomX')).toBe(1)
  expect(writes('chartYRange')).toBe(1)

  // Pan: 6 pohybů, zápis pásma Y až na pointerup; zoom X se nemění
  setItem.mockClear()
  fireEvent.pointerDown(overlay, { ...CENTER, pointerId: 1 })
  for (let step = 1; step <= 6; step += 1) {
    fireEvent.pointerMove(overlay, { clientX: 600 + step * 10, clientY: 300 + step * 5, pointerId: 1 }) // prettier-ignore
    runFrame()
  }
  expect(writes('chartYRange')).toBe(0)
  fireEvent.pointerUp(overlay, { clientX: 660, clientY: 330, pointerId: 1 })
  runFrame()
  expect(writes('chartYRange')).toBe(1)
  expect(writes('chartZoomX')).toBe(0)
})
