/** Testy živého přenačítání intraday dat (issue #125). */
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { useDayData } from './useDayData'
import { fetchDays, fetchReplay } from './loader'
import type { ReplayDay } from './loader'

vi.mock('./loader', () => ({
  fetchReplay: vi.fn(),
  fetchDays: vi.fn(),
}))

function makeDay(minutes: number): ReplayDay {
  return {
    symbol: 'NQ',
    expiry: '20260720',
    date: '2026-07-20',
    grid: { minutes, strikes: [1], layers: {} },
    raw: null,
    overlays: { price: [] },
    panels: {
      vol: [],
      optVolCall: [],
      optVolPut: [],
      cumDelta: [],
      deltaFlowCall: [],
      deltaFlowPut: [],
    },
    profileByMinute: [],
    minutes: Array.from(
      { length: minutes },
      (_, i) => `2026-07-20T${String(9 + i).padStart(2, '0')}:00:00Z`,
    ),
  } as unknown as ReplayDay
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

test('intraday: /replay se přenačítá živě à 60 s (issue #125)', async () => {
  vi.mocked(fetchReplay).mockResolvedValue(makeDay(5))
  renderHook(() => useDayData('NQ', '20260720', '2026-07-20', 'intraday'))
  expect(fetchReplay).toHaveBeenCalledTimes(1) // úvodní fetch při načtení

  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000)
  })
  expect(fetchReplay).toHaveBeenCalledTimes(2) // po minutě znovu

  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000)
  })
  expect(fetchReplay).toHaveBeenCalledTimes(3)
})

test('daily: žádné živé přenačítání intraday balíku', async () => {
  vi.mocked(fetchDays).mockResolvedValue([])
  renderHook(() => useDayData('NQ', '20260720', '2026-07-20', 'daily'))
  await act(async () => {
    await vi.advanceTimersByTimeAsync(180_000)
  })
  expect(fetchReplay).not.toHaveBeenCalled()
})
