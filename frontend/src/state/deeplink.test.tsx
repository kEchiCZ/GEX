/** Test deep-linku: počáteční obrazovka a téma z URL (?view=…&theme=…). */
import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'

beforeEach(() => {
  FakeWebSocket.reset()
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ expiries: [] }) }),
  )
})

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

function renderApp() {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  return render(<App socket={socket} />)
}

test('?view=console otevře konzoli, ?theme=light aplikuje světlé téma', () => {
  window.history.replaceState(null, '', '/?view=console&theme=light')
  renderApp()

  expect(screen.getByLabelText('IBKR Console')).toBeDefined()
  expect(document.querySelector('.app')?.getAttribute('data-theme')).toBe('light')
})

test('neplatný view spadne na výchozí graf', () => {
  window.history.replaceState(null, '', '/?view=teleport')
  renderApp()

  expect(screen.getByLabelText('Heatmapa')).toBeDefined()
  expect(document.querySelector('.app')?.getAttribute('data-theme')).toBe('dark')
})
