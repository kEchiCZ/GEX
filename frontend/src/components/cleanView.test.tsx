/** Testy presetu „Čistý pohled" (#238): zapnutí přepíše volby, vypnutí vrátí
snímek přesně (včetně nevýchozích hodnot), obojí přežije refresh (ADR-0007). */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { LiveSocket } from '../api/ws'
import { FakeWebSocket } from '../test/fakeWs'
import { revivedCleanView, snapshotCleanView, CLEAN_VIEW_OFF } from '../state/cleanView'

// Heatmapa kreslí na canvas (v jsdom neběží) — mock vypíše jména linií, ať jde
// ověřit zúžení úrovní na Max Pain
vi.mock('./Heatmap', () => ({
  Heatmap: ({ overlays }: { overlays?: { levels?: Array<{ name: string }> } }) => (
    <div data-testid="heatmap-level-names">
      {(overlays?.levels ?? []).map((line) => line.name).join(' ')}
    </div>
  ),
}))

function mockApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: unknown) => {
      const target = String(url)
      if (target.includes('/setups/')) {
        return { ok: true, json: async () => ({ symbol: 'ES', setups: [] }) }
      }
      if (target.includes('/expiries')) {
        return { ok: true, json: async () => ({ expiries: ['20260717'] }) }
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

function renderApp() {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  return render(<App socket={socket} />)
}

const wallsSelect = () => screen.getByLabelText('Walls mód') as HTMLSelectElement
const sessionsCheckbox = () => screen.getByLabelText('Sessions') as HTMLInputElement
const layerCheckbox = (label: string) => {
  fireEvent.click(screen.getByRole('button', { name: 'Výběr vrstev grafu' }))
  return screen.getByLabelText(label) as HTMLInputElement
}
const stored = (name: string) => JSON.parse(window.localStorage.getItem(`gexlens.${name}`) ?? 'null')

beforeEach(() => {
  FakeWebSocket.reset()
  vi.restoreAllMocks()
  mockApi()
})

test('zapnutí přepíše volby, vypnutí vrátí snímek včetně nevýchozích hodnot', async () => {
  // Nevýchozí výchozí stav: Ridge vše, Sessions zapnuté, 2. zeď vypnutá
  window.localStorage.setItem('gexlens.walls', JSON.stringify('ridge'))
  window.localStorage.setItem('gexlens.contours', JSON.stringify('major'))
  window.localStorage.setItem(
    'gexlens.toggles',
    JSON.stringify({ sessions: true, secondaryWall: false, setups: true }),
  )
  renderApp()
  expect(wallsSelect().value).toBe('ridge')
  expect(sessionsCheckbox().checked).toBe(true)

  const button = screen.getByTestId('clean-view-toggle')
  expect(button.getAttribute('aria-pressed')).toBe('false')
  fireEvent.click(button)

  expect(button.getAttribute('aria-pressed')).toBe('true')
  expect(button.className).toContain('active')
  expect(wallsSelect().value).toBe('ridge_dominant')
  expect(sessionsCheckbox().checked).toBe(false)
  expect((screen.getByLabelText('Setupy') as HTMLInputElement).checked).toBe(false)
  expect((screen.getByLabelText('Projekce') as HTMLInputElement).checked).toBe(false)
  expect(layerCheckbox('GEX Levels').checked).toBe(true)
  expect((screen.getByLabelText('Zdi') as HTMLInputElement).checked).toBe(false)
  expect((screen.getByLabelText('GEX žebřík') as HTMLInputElement).checked).toBe(false)
  // Persistence: příznak i snímek původních voleb
  await waitFor(() => expect(stored('cleanView').active).toBe(true))
  expect(stored('cleanView').snapshot).toMatchObject({
    walls: 'ridge',
    contours: 'major',
    toggles: { sessions: true, secondaryWall: false, setups: true },
  })
  expect(stored('walls')).toBe('ridge_dominant')
  expect(stored('contours')).toBe('off')

  fireEvent.click(button)
  expect(button.getAttribute('aria-pressed')).toBe('false')
  expect(wallsSelect().value).toBe('ridge')
  expect(sessionsCheckbox().checked).toBe(true)
  expect((screen.getByLabelText('Setupy') as HTMLInputElement).checked).toBe(true)
  expect((screen.getByLabelText('2. zeď') as HTMLInputElement).checked).toBe(false)
  await waitFor(() => expect(stored('cleanView')).toEqual({ active: false, snapshot: null }))
  expect(stored('contours')).toBe('major')
})

test('aktivní preset přežije refresh a vypnutí pořád vrací uložený snímek', () => {
  window.localStorage.setItem('gexlens.walls', JSON.stringify('ridge_dominant'))
  window.localStorage.setItem(
    'gexlens.cleanView',
    JSON.stringify({
      active: true,
      snapshot: snapshotCleanView({
        walls: 'peak',
        contours: 'all',
        priceStyle: 'line',
        toggles: {
          dynGex: true,
          secondaryWall: true,
          gexLevels: true,
          ladder: true,
          flowAdjusted: false,
          sessions: true,
          vol: true,
          optVol: true,
          delta: true,
          deltaFlow: false,
          evoOi: false,
          volOiDelta: true,
          sentiment: false,
          projection: true,
          news: true,
          setups: true,
        },
      }),
    }),
  )
  renderApp()
  const button = screen.getByTestId('clean-view-toggle')
  expect(button.getAttribute('aria-pressed')).toBe('true')
  fireEvent.click(button)
  expect(wallsSelect().value).toBe('peak')
  expect(sessionsCheckbox().checked).toBe(true)
  expect((screen.getByLabelText('Styl ceny') as HTMLSelectElement).value).toBe('line')
})

test('reviver: aktivní stav bez platného snímku spadne na vypnuto', () => {
  const revive = revivedCleanView()
  expect(revive({ active: true, snapshot: null }, CLEAN_VIEW_OFF)).toEqual(CLEAN_VIEW_OFF)
  expect(revive({ active: true, snapshot: { walls: 'nope' } }, CLEAN_VIEW_OFF)).toEqual(
    CLEAN_VIEW_OFF,
  )
  expect(revive('garbage', CLEAN_VIEW_OFF)).toEqual(CLEAN_VIEW_OFF)
  expect(revive({ active: false, snapshot: null }, CLEAN_VIEW_OFF)).toEqual(CLEAN_VIEW_OFF)
})
