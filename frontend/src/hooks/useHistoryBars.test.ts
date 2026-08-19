/** Lazy dotahování historie (#788): kráčení zpět, přeskočení víkendu, konec archivu. */
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useHistoryBars } from './useHistoryBars'

function okPayload(date: string) {
  return {
    ok: true,
    status: 200,
    json: async () => ({
      bars: [{ ts_min: `${date}T00:00:00+00:00`, open: 1, high: 2, low: 0, close: 1, volume: 5 }],
    }),
  }
}

const notFound = { ok: false, status: 404, json: async () => ({}) }

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useHistoryBars', () => {
  it('dotáhne předchozí seanci a víkendové 404 přeskočí', async () => {
    const requested: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const date = String(url).split('date=')[1]
        requested.push(date)
        // Pondělí 17. 8. je seance; 18. 8. taky; víkend 15.–16. 8. je 404
        if (date === '2026-08-16' || date === '2026-08-15') return notFound
        return okPayload(date)
      }),
    )
    const { result } = renderHook(() => useHistoryBars('ES', '2026-08-19'))

    act(() => result.current.requestMore())
    await waitFor(() => expect(result.current.days).toHaveLength(1))
    expect(result.current.days[0].date).toBe('2026-08-18')

    // Druhé dotažení: přeskočí neděli a sobotu, vezme pátek… tady pondělí 17.
    act(() => result.current.requestMore())
    await waitFor(() => expect(result.current.days).toHaveLength(2))
    expect(result.current.days[1].date).toBe('2026-08-17')
    expect(requested).toEqual(['2026-08-18', '2026-08-17'])

    act(() => result.current.requestMore())
    await waitFor(() => expect(result.current.days).toHaveLength(3))
    expect(result.current.days[2].date).toBe('2026-08-14') // 16.+15. přeskočeny
  })

  it('pět děr v řadě znamená konec archivu', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => notFound),
    )
    const { result } = renderHook(() => useHistoryBars('ES', '2026-08-19'))

    act(() => result.current.requestMore())
    await waitFor(() => expect(result.current.exhausted).toBe(true))
    expect(result.current.days).toEqual([])

    // Vyčerpaný archiv už nefetchuje
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>
    const calls = fetchMock.mock.calls.length
    act(() => result.current.requestMore())
    expect(fetchMock.mock.calls.length).toBe(calls)
  })

  it('změna instrumentu resetuje načtenou historii', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => okPayload(String(url).split('date=')[1])),
    )
    const { result, rerender } = renderHook(({ symbol }) => useHistoryBars(symbol, '2026-08-19'), {
      initialProps: { symbol: 'ES' },
    })
    act(() => result.current.requestMore())
    await waitFor(() => expect(result.current.days).toHaveLength(1))

    rerender({ symbol: 'NQ' })
    expect(result.current.days).toEqual([])
  })

  it('síťová chyba nezamkne dotahování — příští pokus jede znovu', async () => {
    let fail = true
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (fail) throw new Error('offline')
        return okPayload(String(url).split('date=')[1])
      }),
    )
    const { result } = renderHook(() => useHistoryBars('ES', '2026-08-19'))

    act(() => result.current.requestMore())
    await waitFor(() => expect(result.current.days).toHaveLength(0))

    fail = false
    act(() => result.current.requestMore())
    await waitFor(() => expect(result.current.days).toHaveLength(1))
  })
})
