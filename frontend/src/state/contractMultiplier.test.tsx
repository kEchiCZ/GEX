/** Multiplikátor kontraktu z API (#1400): `/instruments/{symbol}/expiries` → AppState. */
import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { AppStateProvider, useAppState } from './AppState'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'

const MULTIPLIERS: Record<string, number | null> = { ES: 50, NQ: 20, SPY: 100, NG: null }

function Probe() {
  const { contractMultiplier, setSymbol } = useAppState()
  return (
    <>
      <span data-testid="multiplier">{contractMultiplier ?? '—'}</span>
      {Object.keys(MULTIPLIERS).map((symbol) => (
        <button key={symbol} onClick={() => setSymbol(symbol)}>
          {symbol}
        </button>
      ))}
    </>
  )
}

beforeEach(() => {
  FakeWebSocket.reset()
  window.localStorage.clear()
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      const match = /\/instruments\/([A-Z]+)\/expiries/.exec(url)
      const body = match
        ? { expiries: ['20261013'], detail: [], multiplier: MULTIPLIERS[match[1]] }
        : {}
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) })
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

test('multiplikátor jde z API per symbol — ES 50, NQ 20, SPY 100, neznámý null', async () => {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(
    <AppStateProvider socket={socket}>
      <Probe />
    </AppStateProvider>,
  )
  await waitFor(() => expect(screen.getByTestId('multiplier').textContent).toBe('50'))
  for (const [symbol, expected] of [
    ['NQ', '20'],
    ['SPY', '100'],
    ['NG', '—'],
  ] as const) {
    act(() => screen.getByText(symbol).click())
    await waitFor(() => expect(screen.getByTestId('multiplier').textContent).toBe(expected))
  }
})
