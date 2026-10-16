/** Statická vrstva grafu a kontury z workeru: jediné kolo `drawStatic` po
přepnutí Mode/Scale (#1420) a pozdě doručené kontury flipu (#1530).

Kontury jdou přes podvržený Worker, aby šlo řídit pořadí doručení. Výsledek
počítá týž kód jako skutečný worker (`computeContourPolylines`). Počty
překreslení čte měřič výkonu #1409 a jsou deterministické. */
import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { AnnotationPayload, StoredAnnotation } from '../annotations/model'
import { computeContourPolylines } from '../heatmap/contourCompute'
import type { ContoursMode } from '../heatmap/contours'
import type { ForwardBoundary } from '../heatmap/dailyforward'
import { demoGrid } from '../heatmap/demo'
import type { HeatmapGrid } from '../heatmap/grid'
import type { OverlayData } from '../heatmap/overlays'
import { encodePolylines } from '../heatmap/polylines'
import type { ViewTransform } from '../heatmap/view'
import { perfBegin, perfCounters, resetPerf, setPerfEnabled } from '../perf/meter'
import { CrosshairProvider } from '../state/Crosshair'
import { Heatmap } from './Heatmap'

interface ContourRequest {
  id: number
  buffer: ArrayBuffer
  width: number
  height: number
  mode: ContoursMode
}

/** Worker kontur pod kontrolou testu: požadavky čekají na `deliver`. */
class FakeWorker {
  static instance: FakeWorker | null = null
  requests: ContourRequest[] = []
  private listeners: ((event: { data: { id: number; buffer: ArrayBuffer } }) => void)[] = []

  constructor() {
    FakeWorker.instance = this
  }

  addEventListener(
    type: string,
    listener: (event: { data: { id: number; buffer: ArrayBuffer } }) => void,
  ) {
    // prettier-ignore
    if (type === 'message') this.listeners.push(listener)
  }

  postMessage(request: ContourRequest) {
    this.requests.push(request)
  }

  /** Doručí nejstarší požadavek daného módu jako skutečný worker. */
  deliver(mode: ContoursMode) {
    const index = this.requests.findIndex((request) => request.mode === mode)
    expect(index).toBeGreaterThanOrEqual(0)
    const [request] = this.requests.splice(index, 1)
    const field = new Float32Array(request.buffer)
    const flat = encodePolylines(computeContourPolylines(field, request.width, request.height, mode)) // prettier-ignore
    act(() => {
      for (const listener of this.listeners) listener({ data: { id: request.id, buffer: flat.buffer } }) // prettier-ignore
    })
  }
}

function worker(): FakeWorker {
  const instance = FakeWorker.instance
  if (!instance) throw new Error('worker kontur nevznikl')
  return instance
}

// Stabilní props: výchozí `= []` a `= {}` by dávaly nové identity každý render
const OVERLAYS: OverlayData = {}
const ANNOTATIONS: StoredAnnotation[] = []
const LABELS: string[] = []
const NO_MARKERS: ForwardBoundary[] = []
const VIEW: ViewTransform = { zoomX: 1, zoomY: 1, offsetX: 0, offsetY: 0 }

/** Mřížka jiného módu nad týmiž striky (identita `strikes` drží jako v App). */
function otherMode(grid: HeatmapGrid, factor: number): HeatmapGrid {
  const scale = (layer?: Float32Array) => layer && Float32Array.from(layer, (v) => Math.min(1, v * factor)) // prettier-ignore
  return { ...grid, layers: { call: scale(grid.layers.call), put: scale(grid.layers.put) } }
}

function chart(
  grid: HeatmapGrid,
  contours: ContoursMode,
  extra: { view?: ViewTransform; ghostAnnotations?: AnnotationPayload[] } = {},
) {
  return (
    <CrosshairProvider>
      <Heatmap
        grid={grid}
        style="gradient"
        contours={contours}
        overlays={OVERLAYS}
        annotations={ANNOTATIONS}
        minuteLabels={LABELS}
        forwardMarkers={NO_MARKERS}
        view={extra.view ?? VIEW}
        onViewChange={() => {}}
        ghostAnnotations={extra.ghostAnnotations}
      />
    </CrosshairProvider>
  )
}

beforeEach(() => {
  vi.stubGlobal('Worker', FakeWorker)
  resetPerf()
  setPerfEnabled(true)
})

afterEach(() => {
  setPerfEnabled(false)
  resetPerf()
  if (FakeWorker.instance) FakeWorker.instance.requests = []
  vi.unstubAllGlobals()
})

test('Mode s konturami Major+flip: statická vrstva se kreslí jednou, až po hladinách i flipu', () => {
  const first = demoGrid(120, 40)
  const { rerender } = render(chart(first, 'major+flip'))
  worker().deliver('major')
  worker().deliver('flip')

  resetPerf()
  perfBegin('modeScale')
  rerender(chart(otherMode(first, 1.6), 'major+flip'))
  // Kontury nové mřížky počítá worker: overlay drží poslední kresbu
  expect(perfCounters().drawStatic ?? 0).toBe(0)
  worker().deliver('major')
  expect(perfCounters().drawStatic ?? 0).toBe(0) // flip ještě chybí
  expect(perfCounters().modeScale ?? 0).toBe(0)
  worker().deliver('flip')
  expect(perfCounters()).toMatchObject({ drawStatic: 1, modeScale: 1 })
})

test('kontury flipu doručené po mřížce překreslí statickou vrstvu (#1530)', () => {
  const first = demoGrid(120, 40)
  const { rerender } = render(chart(first, 'flip'))
  worker().deliver('flip')

  resetPerf()
  rerender(chart(otherMode(first, 0.5), 'flip'))
  expect(perfCounters().drawStatic ?? 0).toBe(0)
  worker().deliver('flip')
  expect(perfCounters().drawStatic).toBe(1)
})

test('pan během výpočtu kontur kreslí hned, doručení kontur ještě jednou', () => {
  const first = demoGrid(120, 40)
  const { rerender } = render(chart(first, 'major'))
  worker().deliver('major')

  resetPerf()
  const second = otherMode(first, 1.6)
  rerender(chart(second, 'major'))
  expect(perfCounters().drawStatic ?? 0).toBe(0)
  // Posun pohledu: overlay musí sledovat rastr, i když kontury ještě nejsou
  rerender(chart(second, 'major', { view: { ...VIEW, offsetX: -40 } }))
  expect(perfCounters().drawStatic).toBe(1)
  worker().deliver('major')
  expect(perfCounters().drawStatic).toBe(2)
})

test('bez změny scénářů se statická vrstva nepřekresluje, nové scénáře ano (#1530)', () => {
  const grid = demoGrid(120, 40)
  const { rerender } = render(chart(grid, 'off'))
  resetPerf()
  rerender(chart(grid, 'off'))
  expect(perfCounters().drawStatic ?? 0).toBe(0)

  const ghosts: AnnotationPayload[] = [
    { tool: 'line', color: '#4da3ff', points: [{ minute: 10, strike: 7450 }, { minute: 60, strike: 7470 }] }, // prettier-ignore
  ]
  rerender(chart(grid, 'off', { ghostAnnotations: ghosts }))
  expect(perfCounters().drawStatic).toBe(1)
  rerender(chart(grid, 'off', { ghostAnnotations: ghosts }))
  expect(perfCounters().drawStatic).toBe(1)
})
