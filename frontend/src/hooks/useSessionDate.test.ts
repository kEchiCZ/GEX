/** Obchodní den se překlopí sám přes hranici seance 17:00 CT (#512, #508, #1319). */
import { act, renderHook } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { useSessionDate } from './useSessionDate'

afterEach(() => vi.useRealTimers())

test('po 17:00 CT (22:00 UTC v létě) se den překlopí na zítřejší seanci', () => {
  vi.useFakeTimers()
  vi.setSystemTime(Date.UTC(2026, 8, 28, 21, 59, 30)) // pondělí 16:59:30 CDT
  const { result } = renderHook(() => useSessionDate())
  expect(result.current).toBe('2026-09-28')

  act(() => vi.advanceTimersByTime(60_000)) // 17:00:30 CDT → úterní seance
  expect(result.current).toBe('2026-09-29')
})

test('beze změny dne hook nepřekresluje', () => {
  vi.useFakeTimers()
  vi.setSystemTime(Date.UTC(2026, 8, 28, 15, 0))
  let renders = 0
  renderHook(() => {
    renders += 1
    return useSessionDate()
  })
  const initial = renders
  act(() => vi.advanceTimersByTime(5 * 60_000))
  expect(renders).toBe(initial)
})
