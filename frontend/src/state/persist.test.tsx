/** Testy persistence UI voleb (ADR-0007, #167): revivery, hook, obnovení v App. */
import { render, screen } from '@testing-library/react'
import { act, renderHook } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'
import { clampedNumber, mergedBooleans, oneOf, readStored, shortString, usePersistentState } from './persist' // prettier-ignore

// ── Revivery ───────────────────────────────────────────────────────

test('oneOf: jen hodnota z povolené množiny, jinak fallback', () => {
  const revive = oneOf(['a', 'b'] as const)
  expect(revive('b', 'a')).toBe('b')
  expect(revive('x', 'a')).toBe('a')
  expect(revive(7, 'a')).toBe('a')
})

test('clampedNumber: sevře do intervalu, nečíslo → fallback', () => {
  const revive = clampedNumber(10, 100)
  expect(revive(50, 1)).toBe(50)
  expect(revive(7, 1)).toBe(10)
  expect(revive(500, 1)).toBe(100)
  expect(revive('50', 1)).toBe(1)
  expect(revive(Number.NaN, 1)).toBe(1)
})

test('mergedBooleans: známé klíče přes defaulty, cizí i nebooleanové zahodí', () => {
  const revive = mergedBooleans<{ vol: boolean; news: boolean }>()
  const fallback = { vol: true, news: false }
  expect(revive({ news: true, cizi: true, vol: 'ano' }, fallback)).toEqual({
    vol: true,
    news: true,
  })
  expect(revive(null, fallback)).toBe(fallback)
  expect(revive('rozbité', fallback)).toBe(fallback)
})

test('shortString: neprázdný krátký řetězec, jinak fallback', () => {
  const revive = shortString(4)
  expect(revive('NQ', 'ES')).toBe('NQ')
  expect(revive('', 'ES')).toBe('ES')
  expect(revive('PŘÍLIŠDLOUHÉ', 'ES')).toBe('ES')
})

// ── Hook ───────────────────────────────────────────────────────────

test('usePersistentState: hodnota přežije remount přes localStorage', () => {
  const first = renderHook(() => usePersistentState('interval', '1m', oneOf(['1m', '5m'] as const)))
  act(() => first.result.current[1]('5m'))
  first.unmount()

  const second = renderHook(() =>
    usePersistentState('interval', '1m', oneOf(['1m', '5m'] as const)),
  )
  expect(second.result.current[0]).toBe('5m')
  expect(window.localStorage.getItem('gexlens.interval')).toBe('"5m"')
})

test('usePersistentState: override (URL deep-link) přebíjí uložený stav', () => {
  window.localStorage.setItem('gexlens.priceStyle', '"line"')
  const { result } = renderHook(() =>
    usePersistentState('priceStyle', 'candles', oneOf(['line', 'candles'] as const), 'candles'),
  )
  expect(result.current[0]).toBe('candles')
})

test('readStored: rozbitý JSON i nevalidní hodnota tiše spadnou na default', () => {
  window.localStorage.setItem('gexlens.mode', '{rozbité')
  expect(readStored('mode', 'oi', oneOf(['oi', 'vol_otm'] as const))).toBe('oi')
  window.localStorage.setItem('gexlens.mode', '"neznámý"')
  expect(readStored('mode', 'oi', oneOf(['oi', 'vol_otm'] as const))).toBe('oi')
})

// ── Obnovení v App ─────────────────────────────────────────────────

function mockApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      const target = String(url)
      if (target.includes('/expiries')) {
        return { ok: true, json: async () => ({ expiries: ['20260716'] }) }
      }
      if (target.includes('/watchlist')) {
        return { ok: true, json: async () => ({ watchlist: [{ id: 1, symbol: 'ES' }] }) }
      }
      if (target.includes('/annotations')) {
        return { ok: true, json: async () => ({ annotations: [] }) }
      }
      return { ok: false, status: 404, json: async () => ({}) }
    }),
  )
}

beforeEach(() => {
  FakeWebSocket.reset()
  vi.restoreAllMocks()
})

test('App po refreshi naskočí s uloženými volbami (#167)', () => {
  window.localStorage.setItem('gexlens.interval', '"15m"')
  window.localStorage.setItem('gexlens.mode', '"vol_otm"')
  window.localStorage.setItem('gexlens.scale', '"sqrt"')
  window.localStorage.setItem('gexlens.toggles', JSON.stringify({ sessions: true, vol: false }))
  window.localStorage.setItem('gexlens.priceStyle', '"line"')
  mockApi()
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(<App socket={socket} />)

  expect(screen.getByRole('button', { name: '15m' }).className).toContain('active')
  expect((screen.getByLabelText('Heatmap mód') as HTMLSelectElement).value).toBe('vol_otm')
  expect((screen.getByLabelText('Škála heatmapy') as HTMLSelectElement).value).toBe('sqrt')
  expect((screen.getByLabelText('Styl ceny') as HTMLSelectElement).value).toBe('line')
  expect((screen.getByLabelText('Sessions') as HTMLInputElement).checked).toBe(true)
  expect((screen.getByLabelText('Vol') as HTMLInputElement).checked).toBe(false)
  // Neuložené volby drží default
  expect((screen.getByLabelText('Walls mód') as HTMLSelectElement).value).toBe('off')
})
