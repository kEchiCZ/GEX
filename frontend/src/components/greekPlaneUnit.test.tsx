/** Jednotka Dyn ploch v řádku přepínačů (#1401): přepínač jen u Dyn GEX,
Charm a Vanna mají pevný popisek dolarové delty. */
import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { TogglesRow } from './ControlRows'
import { AppStateProvider } from '../state/AppState'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'

function renderRow(plane: string) {
  window.localStorage.setItem('gexlens.underlayPlane', JSON.stringify(plane))
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(
    <AppStateProvider socket={socket}>
      <TogglesRow />
    </AppStateProvider>,
  )
}

beforeEach(() => {
  FakeWebSocket.reset()
  window.localStorage.clear()
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ expiries: [] }) })),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

test('Dyn GEX má přepínač $/bod a $/1 %', () => {
  renderRow('gex')
  expect(screen.getByLabelText('Jednotka Dyn plochy')).toBeTruthy()
  expect(screen.queryByTestId('greek-plane-unit')).toBeNull()
})

test('Dyn Charm a Dyn Vanna: pevná dolarová delta, bez přepínače (#1401)', () => {
  renderRow('charm')
  expect(screen.queryByLabelText('Jednotka Dyn plochy')).toBeNull()
  const charm = screen.getByTestId('greek-plane-unit')
  expect(charm.textContent).toContain('$ delty / den')
  expect(charm.getAttribute('title')).toContain('\n• ')
})

test('Dyn Vanna: $ delty na 1 bod IV (#1401)', () => {
  renderRow('vanna')
  expect(screen.getByTestId('greek-plane-unit').textContent).toContain('$ delty / 1 bod IV')
})
