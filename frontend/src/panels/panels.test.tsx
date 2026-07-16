/** Testy spodních panelů (issue #26): layout dle checkboxů, C/P barvy, Cum Δ plochy, sync. */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../App'
import { LiveSocket } from '../api/ws'
import { BottomPanels } from '../components/BottomPanels'
import { FakeWebSocket } from '../test/fakeWs'
import { CrosshairProvider, useCrosshair } from '../state/Crosshair'
import { cumDeltaAreas, barHeights } from './geometry'
import type { PanelSeries } from '../components/BottomPanels'

const DATA: PanelSeries = {
  vol: [100, 200, 400, 300],
  optVolCall: [10, 20, 40, 30],
  optVolPut: [15, 5, 25, 35],
  cumDelta: [50, -100, 200, -50],
}

beforeEach(() => {
  FakeWebSocket.reset()
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ expiries: [] }) }),
  )
})

// ── Geometrie ──────────────────────────────────────────────────────

test('barHeights normalizuje maximem', () => {
  expect(barHeights([100, 200, 400], 80)).toEqual([20, 40, 80])
})

test('cumDeltaAreas dělí plochu nad/pod nulou', () => {
  const areas = cumDeltaAreas([100, -100], 200, 80)
  expect(areas.zeroY).toBe(40)
  // Kladná plocha: první bod nad nulou (y=0), druhý na nule
  expect(areas.positive).toContain('50,0')
  expect(areas.positive).toContain('150,40')
  // Záporná plocha: druhý bod pod nulou (y=80)
  expect(areas.negative).toContain('150,80')
})

// ── Panely: sdílená osa, C/P barvy, plochy ─────────────────────────

function renderPanels(visible = { vol: true, optVol: true, delta: true }) {
  return render(
    <CrosshairProvider>
      <BottomPanels data={DATA} visible={visible} width={400} />
    </CrosshairProvider>,
  )
}

test('vykreslí tři panely; Opt Vol má C/P sloupce, Cum Δ plochy a nulu', () => {
  renderPanels()
  expect(screen.getByLabelText('Vol panel')).toBeDefined()
  const optVol = screen.getByLabelText('Opt Vol panel')
  expect(optVol.querySelectorAll('[data-part="optvol-call"]')).toHaveLength(4)
  expect(optVol.querySelectorAll('[data-part="optvol-put"]')).toHaveLength(4)
  const cumDelta = screen.getByLabelText('Cum Δ panel')
  expect(cumDelta.querySelector('[data-part="cumdelta-positive"]')).not.toBeNull()
  expect(cumDelta.querySelector('[data-part="cumdelta-negative"]')).not.toBeNull()
  expect(screen.getByTestId('cumdelta-zero')).toBeDefined()
})

test('vypnutí panelu přeskládá layout (AC)', () => {
  const { rerender } = renderPanels()
  expect(screen.getAllByRole('region')).toHaveLength(3)

  rerender(
    <CrosshairProvider>
      <BottomPanels data={DATA} visible={{ vol: false, optVol: true, delta: true }} width={400} />
    </CrosshairProvider>,
  )
  expect(screen.queryByLabelText('Vol panel')).toBeNull()
  expect(screen.getAllByRole('region')).toHaveLength(2)

  rerender(
    <CrosshairProvider>
      <BottomPanels data={DATA} visible={{ vol: false, optVol: false, delta: false }} width={400} />
    </CrosshairProvider>,
  )
  expect(screen.queryByLabelText('Spodní panely')).toBeNull() // nic nezbylo
})

test('checkboxy v horní liště řídí panely (integrace přes App)', async () => {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(<App socket={socket} />)

  expect(screen.getByLabelText('Vol panel')).toBeDefined()
  fireEvent.click(screen.getByLabelText('Vol'))
  expect(screen.queryByLabelText('Vol panel')).toBeNull()
  expect(screen.getByLabelText('Opt Vol panel')).toBeDefined() // ostatní zůstávají
})

// ── Crosshair sdílený s heatmapou ──────────────────────────────────

function Reader() {
  const { position } = useCrosshair()
  return <output data-testid="reader">{position ? position.minuteIdx : 'none'}</output>
}

test('pohyb v panelu nastaví minutu crosshairu; linka se kreslí ve všech panelech', () => {
  render(
    <CrosshairProvider>
      <BottomPanels data={DATA} visible={{ vol: true, optVol: true, delta: true }} width={400} />
      <Reader />
    </CrosshairProvider>,
  )
  const volSvg = screen.getByLabelText('Vol panel').querySelector('svg')!
  // šířka 400, 4 minuty → krok 100; x=250 → minuta 2
  fireEvent.pointerMove(volSvg, { clientX: 250, clientY: 40 })

  expect(screen.getByTestId('reader').textContent).toBe('2')
  const lines = screen.getAllByTestId('panel-crosshair')
  expect(lines).toHaveLength(3) // sdílená osa X — linka ve všech panelech
  for (const line of lines) {
    expect(Number(line.getAttribute('x1'))).toBe(250)
  }
})
