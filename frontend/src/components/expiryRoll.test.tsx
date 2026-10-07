/** Přechod výchozí expirace v jejím settle (#1367) — protějšek rollu enginu (#1331). */
import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { AppStateProvider, useAppState } from '../state/AppState'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'

// Středa 7. 10. 2026 15:59 CDT — minuta před settle 16:00 ET (20:00 UTC)
const BEFORE_SETTLE = Date.UTC(2026, 9, 7, 19, 59)

function Selected() {
  const { selectedExpiry, setSelectedExpiry } = useAppState()
  return (
    <>
      <span data-testid="expiry">{selectedExpiry ?? '—'}</span>
      <button onClick={() => setSelectedExpiry('20261007')}>ručně</button>
    </>
  )
}

function renderWithExpiries() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      const body = url.includes('/expiries')
        ? { expiries: ['20261007', '20261008'] }
        : url.includes('/days')
          ? {
              days: [
                { date: '2026-10-07', expiry: '20261007' },
                { date: '2026-10-07', expiry: '20261008' },
              ],
            }
          : {}
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) })
    }),
  )
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(
    <AppStateProvider socket={socket}>
      <Selected />
    </AppStateProvider>,
  )
}

beforeEach(() => {
  FakeWebSocket.reset()
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'], shouldAdvanceTime: true })
  vi.setSystemTime(BEFORE_SETTLE)
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

test('automaticky zvolená 0DTE se v settle přepne na další expiraci', async () => {
  renderWithExpiries()
  await waitFor(() => expect(screen.getByTestId('expiry').textContent).toBe('20261007'))
  await act(async () => {
    await vi.advanceTimersByTimeAsync(61_000)
  })
  await waitFor(() => expect(screen.getByTestId('expiry').textContent).toBe('20261008'))
})

test('ručně zvolenou expiraci settle nepřepíná', async () => {
  renderWithExpiries()
  await waitFor(() => expect(screen.getByTestId('expiry').textContent).toBe('20261007'))
  act(() => screen.getByText('ručně').click())
  await act(async () => {
    await vi.advanceTimersByTimeAsync(61_000)
  })
  expect(screen.getByTestId('expiry').textContent).toBe('20261007')
})
