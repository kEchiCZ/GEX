/** Výchozí filtr zpráv v grafu = „Významné“ i při dřív uložené volbě „Vše“ (#1305). */
import { renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, expect, test, vi } from 'vitest'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'
import { AppStateProvider, NEWS_MARKER_FILTER_KEY, useAppState } from './AppState'

afterEach(() => {
  window.localStorage.clear()
  vi.unstubAllGlobals()
})

function wrapper({ children }: { children: ReactNode }) {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ expiries: [] }) }),
  )
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  return <AppStateProvider socket={socket}>{children}</AppStateProvider>
}

test('stará uložená volba „Vše“ nový výchozí stav nepřebije (nový klíč)', () => {
  // Klíč před #1305 — tehdy byl výchozí stav „Vše“
  window.localStorage.setItem('gexlens.newsMarkerFilter', JSON.stringify('all'))
  const { result } = renderHook(() => useAppState(), { wrapper })
  expect(NEWS_MARKER_FILTER_KEY).toBe('newsMarkerFilter.v2')
  expect(result.current.newsMarkerFilter).toBe('important')
})

test('volba „Vše“ pod novým klíčem se pamatuje', () => {
  window.localStorage.setItem(`gexlens.${NEWS_MARKER_FILTER_KEY}`, JSON.stringify('all'))
  const { result } = renderHook(() => useAppState(), { wrapper })
  expect(result.current.newsMarkerFilter).toBe('all')
})
