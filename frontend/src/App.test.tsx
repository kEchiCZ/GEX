import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from './App'
import { LiveSocket } from './api/ws'
import { FakeWebSocket } from './test/fakeWs'

function makeApp() {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  return render(<App socket={socket} />)
}

beforeEach(() => {
  FakeWebSocket.reset()
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ expiries: ['20260716', '20260717'] }),
    }),
  )
})

test('vykreslí kompletní layout (SPEC 7.1)', async () => {
  makeApp()

  expect(screen.getAllByText('ES').length).toBeGreaterThan(0) // ticker + watchlist
  expect(screen.getByLabelText('Hlavní navigace')).toBeDefined()
  expect(screen.getByLabelText('Watchlist')).toBeDefined()
  expect(screen.getByLabelText('Timeframe')).toBeDefined()
  expect(screen.getByLabelText('Přepínače vizualizace')).toBeDefined()
  expect(screen.getByLabelText('Stav pipeline')).toBeDefined()
  expect(screen.getByText('Dyn GEX')).toBeDefined()
  expect(screen.getByText('Vol + OI Δ')).toBeDefined()

  // Expirace načtené z REST
  expect(await screen.findByRole('option', { name: '20260716' })).toBeDefined()
})

test('sidebar se dá sbalit a rozbalit', () => {
  makeApp()
  const toggle = screen.getByLabelText('Sbalit menu')

  fireEvent.click(toggle)
  expect(screen.queryByLabelText('Hlavní navigace')).toBeNull()

  fireEvent.click(screen.getByLabelText('Rozbalit menu'))
  expect(screen.getByLabelText('Hlavní navigace')).toBeDefined()
})

test('stavová lišta žije ze status kanálu /ws/live (AC)', () => {
  makeApp()
  expect(screen.getByTestId('status-live').textContent).toBe('Stale')

  const ws = FakeWebSocket.latest()
  act(() => {
    ws.open()
    ws.push('status', {
      engine: 'online',
      connection: 'connected',
      port: 7496,
      greeks_complete: 350,
      greeks_total: 360,
      repair_count: 4,
      lines_utilization: 0.8,
      disk_usage_bytes: 500 * 1024 * 1024,
      disk_limit_bytes: 2 * 1024 * 1024 * 1024,
    })
  })

  expect(screen.getByTestId('status-greeks').textContent).toBe('Greeks 350/360')
  expect(screen.getByTestId('status-repair').textContent).toBe(
    'Repair: retrying 4 incomplete strikes',
  )
  expect(screen.getByTestId('status-lines').textContent).toBe('Lines 80 %')
  expect(screen.getByTestId('status-ibkr').textContent).toBe('IBKR: connected :7496')
  expect(screen.getByTestId('status-disk').textContent).toBe('Disk 500.0 MB / 2.0 GB')
  expect(screen.getByTestId('status-live').textContent).toContain('● Live')
})

test('přepínače timeframe a vizualizace mění stav', () => {
  makeApp()

  const daily = screen.getByRole('button', { name: 'Daily' })
  fireEvent.click(daily)
  expect(daily.className).toContain('active')

  const sessions = screen.getByLabelText('Sessions') as HTMLInputElement
  expect(sessions.checked).toBe(false)
  fireEvent.click(sessions)
  expect(sessions.checked).toBe(true)
})
