/** Test deep-linku: počáteční obrazovka a téma z URL (?view=…&theme=…). */
import { cleanup, render, screen } from '@testing-library/react'
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

test('?view=settings otevře Settings, zrušený ?view=console padá na graf (#705)', () => {
  window.history.replaceState(null, '', '/?view=settings&theme=light')
  renderApp()

  expect(screen.getByLabelText('Settings')).toBeDefined()
  expect(document.querySelector('.app')?.getAttribute('data-theme')).toBe('light')

  cleanup()
  window.history.replaceState(null, '', '/?view=console')
  renderApp()
  // Console zrušena — starý deep-link nesmí spadnout, reviver vrací graf
  expect(screen.getByLabelText('Heatmapa')).toBeDefined()
})

test('neplatný view spadne na výchozí graf', () => {
  window.history.replaceState(null, '', '/?view=teleport')
  renderApp()

  expect(screen.getByLabelText('Heatmapa')).toBeDefined()
  expect(document.querySelector('.app')?.getAttribute('data-theme')).toBe('dark')
})
