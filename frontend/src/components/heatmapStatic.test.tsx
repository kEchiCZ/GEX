/** Statická vrstva grafu a kontury z workeru: jediné kolo `drawStatic` po
doručení kontur (#1420) a pozdě doručené kontury flipu (#1530).

Kontury jdou přes podvržený Worker, aby šlo řídit doručení. Výsledek počítá
týž kód jako skutečný worker (`computeContourPolylinesForModes`). Počty
překreslení čte měřič výkonu #1409 a jsou deterministické. */
import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { AnnotationPayload, StoredAnnotation } from '../annotations/model'
import { computeContourPolylinesForModes } from '../heatmap/contourCompute'
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
  modes: ContoursMode[]
  /** Počet kreseb statické vrstvy v okamžiku odeslání úlohy. */
  drawsAtPost: number
}

type Listener = (event: { data: { id: number; buffers: ArrayBuffer[] } }) => void

/** Worker kontur pod kontrolou testu: požadavky čekají na `deliver`. */
class FakeWorker {
  static instance: FakeWorker | null = null
  requests: ContourRequest[] = []
  private listeners: Listener[] = []

  constructor() {
    FakeWorker.instance = this
  }

  addEventListener(type: string, listener: Listener) {
    if (type === 'message') this.listeners.push(listener)
  }

  postMessage(request: Omit<ContourRequest, 'drawsAtPost'>) {
    this.requests.push({ ...request, drawsAtPost: perfCounters().drawStatic ?? 0 })
  }

  /** Doručí nejstarší požadavek (všechny jeho módy) jako skutečný worker. */
  deliver() {
    const request = this.requests.shift()
    if (!request) throw new Error('žádný požadavek kontur nečeká')
    const field = new Float32Array(request.buffer)
    const sets = computeContourPolylinesForModes(field, request.width, request.height, request.modes) // prettier-ignore
    const buffers = sets.map((polylines) => encodePolylines(polylines).buffer)
    act(() => {
      for (const listener of this.listeners) listener({ data: { id: request.id, buffers } })
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
  extra: { overlays?: OverlayData; ghostAnnotations?: AnnotationPayload[] } = {},
) {
  return (
    <CrosshairProvider>
      <Heatmap
        grid={grid}
        style="gradient"
        contours={contours}
        overlays={extra.overlays ?? OVERLAYS}
        annotations={ANNOTATIONS}
        minuteLabels={LABELS}
        forwardMarkers={NO_MARKERS}
        view={VIEW}
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

test('Mode s konturami Major+flip: jedna úloha a po jejím doručení jedno kolo drawStatic', () => {
  const first = demoGrid(120, 40)
  const { rerender } = render(chart(first, 'major+flip'))
  expect(worker().requests.map((request) => request.modes)).toEqual([['major', 'flip']])
  worker().deliver()

  resetPerf()
  perfBegin('modeScale')
  rerender(chart(otherMode(first, 1.6), 'major+flip'))
  // Hladiny i flip nové mřížky jednou úlohou nad sdíleným blurem
  expect(worker().requests.map((request) => request.modes)).toEqual([['major', 'flip']])
  // Data overlay se nezměnila, takže do doručení se statická vrstva nekreslí
  expect(perfCounters().drawStatic ?? 0).toBe(0)
  expect(perfCounters().modeScale ?? 0).toBe(0)
  worker().deliver()
  expect(perfCounters()).toMatchObject({ drawStatic: 1, modeScale: 1 })
})

test('úloha kontur odchází před kresbou statické vrstvy (souběžně s ní)', () => {
  const first = demoGrid(120, 40)
  const { rerender } = render(chart(first, 'major'))
  worker().deliver()

  resetPerf()
  // Nová mřížka s novými daty overlay (jako minutový update): kreslí se hned
  rerender(chart(otherMode(first, 0.7), 'major', { overlays: {} }))
  expect(perfCounters().drawStatic).toBe(1)
  expect(worker().requests).toHaveLength(1)
  expect(worker().requests[0].drawsAtPost).toBe(0) // úloha odešla dřív než kresba
  worker().deliver()
  expect(perfCounters().drawStatic).toBe(2) // po konturách nejvýš jedno kolo
})

test('nové mřížky s daty se kreslí hned, bez čekání na kontury (přehrávání)', () => {
  const first = demoGrid(120, 40)
  const { rerender } = render(chart(first, 'major+flip'))
  worker().deliver()

  resetPerf()
  // Tažení pozice přehrávání: každá pozice = nová mřížka a nová data overlay
  for (const factor of [0.9, 0.8, 0.7]) {
    rerender(chart(otherMode(first, factor), 'major+flip', { overlays: {} }))
  }
  expect(perfCounters().drawStatic).toBe(3)
})

test('kontury flipu doručené po mřížce překreslí statickou vrstvu (#1530)', () => {
  const first = demoGrid(120, 40)
  const { rerender } = render(chart(first, 'flip'))
  worker().deliver()

  resetPerf()
  rerender(chart(otherMode(first, 0.5), 'flip'))
  expect(perfCounters().drawStatic ?? 0).toBe(0)
  worker().deliver()
  expect(perfCounters().drawStatic).toBe(1)
})

test('bez změny scénářů se statická vrstva nepřekresluje, nové scénáře ano (#1530)', () => {
  const grid = demoGrid(120, 40)
  const { rerender } = render(chart(grid, 'off'))
  resetPerf()
  rerender(chart(grid, 'off'))
  expect(perfCounters().drawStatic ?? 0).toBe(0)

  const ghosts: AnnotationPayload[] = [
    {
      tool: 'line',
      color: '#4da3ff',
      points: [
        { minute: 10, strike: 7450 },
        { minute: 60, strike: 7470 },
      ],
    },
  ]
  rerender(chart(grid, 'off', { ghostAnnotations: ghosts }))
  expect(perfCounters().drawStatic).toBe(1)
  rerender(chart(grid, 'off', { ghostAnnotations: ghosts }))
  expect(perfCounters().drawStatic).toBe(1)
})
