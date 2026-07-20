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
  deltaFlowCall: [5, 10, 20, 15],
  deltaFlowPut: [7, 2, 12, 17],
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

function renderPanels(visible = { vol: true, optVol: true, delta: true, deltaFlow: false }) {
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
      <BottomPanels
        data={DATA}
        visible={{ vol: false, optVol: true, delta: true, deltaFlow: false }}
        width={400}
      />
    </CrosshairProvider>,
  )
  expect(screen.queryByLabelText('Vol panel')).toBeNull()
  expect(screen.getAllByRole('region')).toHaveLength(2)

  rerender(
    <CrosshairProvider>
      <BottomPanels
        data={DATA}
        visible={{ vol: false, optVol: false, delta: false, deltaFlow: false }}
        width={400}
      />
    </CrosshairProvider>,
  )
  expect(screen.queryByLabelText('Spodní panely')).toBeNull() // nic nezbylo
})

test('Δ Flow panel: C/P delta-vážené sloupce, zapíná se checkboxem', () => {
  render(
    <CrosshairProvider>
      <BottomPanels
        data={DATA}
        visible={{ vol: false, optVol: false, delta: false, deltaFlow: true }}
        width={400}
      />
    </CrosshairProvider>,
  )
  const panel = screen.getByLabelText('Δ Flow panel')
  expect(panel.querySelectorAll('[data-part="deltaflow-call"]')).toHaveLength(4)
  expect(panel.querySelectorAll('[data-part="deltaflow-put"]')).toHaveLength(4)
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

test('málo košů se neroztahuje na šířku — ukotvení k pravému okraji (issue #102)', () => {
  const socket = new LiveSocket('ws://test/ws/live', {
    webSocketFactory: (url) => new FakeWebSocket(url),
  })
  render(<App socket={socket} />)
  const volGroup = () => screen.getByLabelText('Vol panel').querySelector('g')!
  // 1m: demo den 390 minut vyplní šířku → fit-to-width beze změny (offset 0)
  expect(volGroup().getAttribute('transform')).toBe('translate(0 0) scale(1 1)')
  // 1h: 7 košů × 12 px (strop) → data u pravého okraje: 1200 − 60 − 7×12 = 1056
  fireEvent.click(screen.getByRole('button', { name: '1h' }))
  expect(volGroup().getAttribute('transform')).toBe('translate(1056 0) scale(1 1)')
})

test('crosshair ukazuje hodnoty ukazatelů vpravo (issue #104)', () => {
  render(
    <CrosshairProvider>
      <BottomPanels
        data={DATA}
        visible={{ vol: true, optVol: true, delta: true, deltaFlow: true }}
        width={400}
      />
    </CrosshairProvider>,
  )
  // Bez crosshairu se hodnoty neukazují
  expect(screen.queryAllByTestId('panel-value')).toHaveLength(0)
  const volSvg = screen.getByLabelText('Vol panel').querySelector('svg')!
  fireEvent.pointerMove(volSvg, { clientX: 30, clientY: 40 }) // krok 12 px → minuta 2
  // vol[2]=400, cumDelta[2]=200(+), optVol C40/P25, deltaFlow C20/P12
  expect(screen.getByLabelText('Vol panel').querySelector('.panel-value')!.textContent).toBe('400')
  expect(screen.getByLabelText('Cum Δ panel').querySelector('.panel-value')!.textContent).toBe(
    '+200',
  )
  const opt = screen.getByLabelText('Opt Vol panel').querySelector('.panel-value')!
  expect(opt.textContent).toContain('C 40')
  expect(opt.textContent).toContain('P 25')
  const flow = screen.getByLabelText('Δ Flow panel').querySelector('.panel-value')!
  expect(flow.textContent).toContain('C 20')
  expect(flow.textContent).toContain('P 12')
})

// ── Crosshair sdílený s heatmapou ──────────────────────────────────

function Reader() {
  const { position } = useCrosshair()
  return <output data-testid="reader">{position ? position.minuteIdx : 'none'}</output>
}

test('pohyb v panelu nastaví minutu crosshairu; linka se kreslí ve všech panelech', () => {
  render(
    <CrosshairProvider>
      <BottomPanels
        data={DATA}
        visible={{ vol: true, optVol: true, delta: true, deltaFlow: false }}
        width={400}
      />
      <Reader />
    </CrosshairProvider>,
  )
  const volSvg = screen.getByLabelText('Vol panel').querySelector('svg')!
  // 4 minuty → krok zastropovaný na 12 px (BUCKET_MAX_PX); x=30 → minuta 2
  fireEvent.pointerMove(volSvg, { clientX: 30, clientY: 40 })

  expect(screen.getByTestId('reader').textContent).toBe('2')
  const lines = screen.getAllByTestId('panel-crosshair')
  expect(lines).toHaveLength(3) // sdílená osa X — linka ve všech panelech
  for (const line of lines) {
    expect(Number(line.getAttribute('x1'))).toBe(30) // (2+0.5) × 12
  }
})

test('panely respektují pan/zoom časové osy hlavního grafu (prop time)', () => {
  render(
    <CrosshairProvider>
      <BottomPanels
        data={DATA}
        visible={{ vol: true, optVol: false, delta: false, deltaFlow: false }}
        width={400}
        time={{ offsetX: 40, zoomX: 2 }}
      />
      <Reader />
    </CrosshairProvider>,
  )
  const svg = screen.getByLabelText('Vol panel').querySelector('svg')!
  // Obsah je v transformované skupině — stejné mapování jako heatmapa
  expect(svg.querySelector('g')?.getAttribute('transform')).toBe('translate(40 0) scale(2 1)')
  // Inverze ukazatele: x=76 → base (76-40)/2 = 18 → minuta 1 (krok 12)
  fireEvent.pointerMove(svg, { clientX: 76, clientY: 40 })
  expect(screen.getByTestId('reader').textContent).toBe('1')
})
