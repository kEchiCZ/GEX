/** Testy svíčkového režimu ceny: geometrie, přepínač a posuvník viditelnosti. */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'
import { candleGeometry } from './overlays'
import type { PriceBar } from './overlays'

const STRIKES = [7590, 7595, 7600, 7605, 7610]

beforeEach(() => {
  FakeWebSocket.reset()
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ expiries: [] }) }),
  )
})

// ── Geometrie svíček ───────────────────────────────────────────────

test('candleGeometry mapuje OHLC na řádky a určuje směr', () => {
  const bars: PriceBar[] = [
    { minuteIdx: 0, close: 7605, up: true, open: 7595, high: 7607.5, low: 7592.5 },
    { minuteIdx: 1, close: 7595, up: false, open: 7605, high: 7605, low: 7590 },
  ]

  const candles = candleGeometry(bars, STRIKES)

  expect(candles).toHaveLength(2)
  expect(candles[0].up).toBe(true) // close > open → zelená
  expect(candles[0].openRow).toBe(1) // 7595
  expect(candles[0].closeRow).toBe(3) // 7605
  expect(candles[0].highRow).toBeCloseTo(3.5) // 7607.5 interpolovaně
  expect(candles[0].lowRow).toBeCloseTo(0.5)
  expect(candles[1].up).toBe(false) // close < open → červená
})

test('bary bez kompletního OHLC se přeskakují (křivková data)', () => {
  const bars: PriceBar[] = [
    { minuteIdx: 0, close: 7600, up: true }, // jen close
    { minuteIdx: 1, close: 7605, up: true, open: 7600, high: 7606, low: 7599 },
  ]
  const candles = candleGeometry(bars, STRIKES)
  expect(candles).toHaveLength(1)
  expect(candles[0].minuteIdx).toBe(1)
})

// ── Ovládání v UI ──────────────────────────────────────────────────

test('přepínač Křivka/Svíčky a posuvník viditelnosti fungují', () => {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(<App socket={socket} />)

  const styleSelect = screen.getByLabelText('Styl ceny') as HTMLSelectElement
  expect(styleSelect.value).toBe('candles') // default svíčky (požadavek uživatele)
  fireEvent.change(styleSelect, { target: { value: 'line' } })
  expect(styleSelect.value).toBe('line')

  const slider = screen.getByLabelText('Viditelnost ceny') as HTMLInputElement
  expect(slider.value).toBe('100')
  fireEvent.change(slider, { target: { value: '35' } })
  expect(slider.value).toBe('35')
  expect(Number(slider.min)).toBe(10) // cena nikdy nezmizí úplně
})
