/** Badge kalibrované FA α ve stavové liště (#232 fáze 2). */
import { render, screen } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { LiveSocket } from '../api/ws'
import { daysLabel } from '../hooks/useFaAlpha'
import { FakeWebSocket } from '../test/fakeWs'

function mockApi(alphas: Array<{ symbol: string; alpha: number; days: number }>) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      const target = String(url)
      if (target.includes('/fa/alpha')) {
        return { ok: true, json: async () => ({ alphas }) }
      }
      if (target.includes('/expiries')) {
        return { ok: true, json: async () => ({ expiries: ['20260716'] }) }
      }
      return { ok: false, status: 404, json: async () => ({}) }
    }),
  )
}

function renderApp() {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  return render(<App socket={socket} />)
}

beforeEach(() => {
  FakeWebSocket.reset()
  vi.restoreAllMocks()
  window.localStorage.clear()
})

test('badge ukáže kalibrovanou α aktivního symbolu s počtem dnů', async () => {
  mockApi([
    { symbol: 'ES', alpha: 0.34, days: 5 },
    { symbol: 'NQ', alpha: 0.48, days: 2 },
  ])
  renderApp()

  const badge = await screen.findByTestId('status-fa-alpha')
  expect(badge.textContent).toBe('FA α=0.34 · 5 dní')
  // Vysvětlení v tooltipu — uživatel musí vědět, co číslo znamená
  expect(badge.getAttribute('title')).toContain('flow-adjusted')
})

test('bez kalibračních bodů se badge nekreslí (engine jede na defaultu)', async () => {
  mockApi([])
  renderApp()

  await screen.findByTestId('status-greeks')
  expect(screen.queryByTestId('status-fa-alpha')).toBeNull()
})

test('daysLabel skloňuje česky', () => {
  expect(daysLabel(1)).toBe('1 den')
  expect(daysLabel(3)).toBe('3 dny')
  expect(daysLabel(5)).toBe('5 dní')
})
