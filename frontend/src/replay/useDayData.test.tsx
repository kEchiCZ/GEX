/** Testy živého intraday: WS append minut (#127) + hodinová pojistka refetch. */
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { LiveSocket } from '../api/ws'
import { useDayData } from './useDayData'
import { fetchDays, fetchReplay, fetchReplayInputs } from './loader'
import type { ReplayInputs } from './loader'

vi.mock('./loader', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./loader')>()
  return { ...actual, fetchReplayInputs: vi.fn(), fetchReplay: vi.fn(), fetchDays: vi.fn() }
})

/** Minimální vstup: 1 strike × 1 minuta. */
function makeInputs(): ReplayInputs {
  return {
    symbol: 'ES',
    expiry: '20260716',
    date: '2026-07-16',
    minutes: ['2026-07-16T15:00:00.000Z'],
    strikes: [7600],
    callOi: new Float32Array([100]),
    putOi: new Float32Array([200]),
    callVolume: new Float32Array([10]),
    putVolume: new Float32Array([5]),
    callDelta: new Float32Array([0.5]),
    putDelta: new Float32Array([-0.4]),
    staleAge: new Float32Array([0]),
    bars: [{ tsIso: '2026-07-16T15:00:00.000Z', close: 7600.5, volume: 1000 }],
    levels: [],
    flow: [],
    oiPrev: [],
  }
}

type FakeSocket = LiveSocket & { emit: (channel: string, data: unknown) => void }

function makeSocket(): FakeSocket {
  const handlers = new Map<string, Set<(data: unknown) => void>>()
  const socket = {
    subscribe: (channel: string, handler: (data: unknown) => void) => {
      let set = handlers.get(channel)
      if (!set) {
        set = new Set()
        handlers.set(channel, set)
      }
      set.add(handler)
    },
    unsubscribe: (channel: string, handler: (data: unknown) => void) =>
      handlers.get(channel)?.delete(handler),
    onReconnect: () => () => {},
    connect: () => {},
    close: () => {},
    emit: (channel: string, data: unknown) => handlers.get(channel)?.forEach((h) => h(data)),
  }
  return socket as unknown as FakeSocket
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

test('WS snapshot+price přidá novou minutu (append, ne refetch) — issue #127', async () => {
  vi.mocked(fetchReplayInputs).mockResolvedValue(makeInputs())
  const socket = makeSocket()
  const { result } = renderHook(() =>
    useDayData('ES', '20260716', '2026-07-16', 'intraday', socket),
  )
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0) // úvodní fetch
  })
  expect(result.current.grid.minutes).toBe(1)
  expect(fetchReplayInputs).toHaveBeenCalledTimes(1)

  await act(async () => {
    socket.emit('snapshot.ES.20260716', {
      ts_min: '2026-07-16T15:01:00Z',
      rows: [
        { strike: 7600, right: 'C', oi: 100, volume: 30, delta: 0.5 },
        { strike: 7600, right: 'P', oi: 200, volume: 12, delta: -0.4 },
      ],
    })
    socket.emit('price.ES', {
      ts: '2026-07-16T15:01:00Z',
      open: 7600.5,
      high: 7603,
      low: 7600,
      close: 7602,
      volume: 1300,
    })
    await vi.advanceTimersByTimeAsync(500) // debounce flush
  })
  expect(result.current.grid.minutes).toBe(2) // minuta přibyla appendem
  expect(fetchReplayInputs).toHaveBeenCalledTimes(1) // bez dalšího refetche
})

test('plný refetch je hodinová pojistka, ne každou minutu — issue #127', async () => {
  vi.mocked(fetchReplayInputs).mockResolvedValue(makeInputs())
  renderHook(() => useDayData('ES', '20260716', '2026-07-16', 'intraday', makeSocket()))
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(fetchReplayInputs).toHaveBeenCalledTimes(1)

  await act(async () => {
    await vi.advanceTimersByTimeAsync(120_000) // 2 minuty → žádný refetch
  })
  expect(fetchReplayInputs).toHaveBeenCalledTimes(1)

  await act(async () => {
    await vi.advanceTimersByTimeAsync(3_600_000) // hodina → pojistný refetch
  })
  expect(fetchReplayInputs).toHaveBeenCalledTimes(2)
})

test('daily režim nepoužívá intraday live fetch', async () => {
  vi.mocked(fetchDays).mockResolvedValue([])
  renderHook(() => useDayData('ES', '20260716', '2026-07-16', 'daily', makeSocket()))
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3_600_000)
  })
  expect(fetchReplayInputs).not.toHaveBeenCalled()
  expect(fetchReplay).not.toHaveBeenCalled()
})
