/** Gesto pohledu (#1419, E-2a.6): kroky panu, tažení os a kolečka se skládají
do jednoho zápisu pohledu na snímek (rAF) a zoom X a pásmo Y se hlásí
k persistenci jednou na konci gesta (pointerup, klid kolečka). */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { Heatmap } from './Heatmap'
import { demoGrid } from '../heatmap/demo'
import { DEFAULT_VIEW, WHEEL_IDLE_MS } from '../heatmap/view'
import type { ViewTransform } from '../heatmap/view'
import { CrosshairProvider } from '../state/Crosshair'

const grid = demoGrid(100, 10)
// Výchozí plátno useElementSize v jsdom: 1200 × 640; střed = plocha grafu
const CENTER = { clientX: 600, clientY: 300 }
// Pruh osy X je u spodní hrany (axisZoneAt)
const X_AXIS = { clientX: 600, clientY: 635 }

// rAF ručně: snímek pustí test, ne čas
let frames = new Map<number, FrameRequestCallback>()
let nextFrame = 1

beforeEach(() => {
  frames = new Map()
  nextFrame = 1
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    const id = nextFrame++
    frames.set(id, callback)
    return id
  })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => {
    frames.delete(id)
  })
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function runFrame() {
  act(() => {
    const pending = [...frames.values()]
    frames = new Map()
    for (const callback of pending) callback(performance.now())
  })
}

function setup(resetKey = 'ES') {
  const onView = vi.fn<(view: ViewTransform) => void>()
  const onZoomX = vi.fn<(zoomX: number) => void>()
  const onYRange = vi.fn<(range: { top: number; bottom: number }) => void>()
  function Harness({ datasetKey }: { datasetKey: string }) {
    const [view, setView] = useState<ViewTransform>(DEFAULT_VIEW)
    return (
      <CrosshairProvider>
        <Heatmap
          grid={grid}
          style="gradient"
          contours="off"
          view={view}
          onViewChange={(next) => {
            onView(next)
            setView(next)
          }}
          onUserZoomX={onZoomX}
          onUserYRange={onYRange}
          resetKey={datasetKey}
        />
      </CrosshairProvider>
    )
  }
  const utils = render(<Harness datasetKey={resetKey} />)
  runFrame() // snímky z mountu (crosshair, kresba) mimo gesto
  onView.mockClear()
  const overlay = screen.getByRole('img', { name: 'GEX heatmapa' })
  const rerender = (datasetKey: string) => utils.rerender(<Harness datasetKey={datasetKey} />)
  return { overlay, onView, onZoomX, onYRange, rerender }
}

test('kroky kolečka v jednom snímku = jeden zápis pohledu, složený ze všech kroků', () => {
  const { overlay, onView } = setup()
  fireEvent.wheel(overlay, { deltaY: -100, ...CENTER })
  fireEvent.wheel(overlay, { deltaY: -100, ...CENTER })
  fireEvent.wheel(overlay, { deltaY: -100, ...CENTER })
  expect(onView).not.toHaveBeenCalled()

  runFrame()
  expect(onView).toHaveBeenCalledTimes(1)
  expect(onView.mock.calls[0][0].zoomX).toBeCloseTo(1.15 ** 3, 10)
  expect(onView.mock.calls[0][0].zoomY).toBeCloseTo(1.15 ** 3, 10)

  // Další krok staví na posledním kroku gesta
  fireEvent.wheel(overlay, { deltaY: 100, ...CENTER })
  runFrame()
  expect(onView).toHaveBeenCalledTimes(2)
  expect(onView.mock.calls[1][0].zoomX).toBeCloseTo(1.15 ** 2, 10)
})

test('kolečko persistuje zoom X i pásmo Y jednou, až po klidu', () => {
  const { overlay, onZoomX, onYRange } = setup()
  for (let step = 0; step < 4; step += 1) {
    fireEvent.wheel(overlay, { deltaY: -100, ...CENTER })
    runFrame()
    act(() => vi.advanceTimersByTime(WHEEL_IDLE_MS / 2))
  }
  expect(onZoomX).not.toHaveBeenCalled()
  expect(onYRange).not.toHaveBeenCalled()

  act(() => vi.advanceTimersByTime(WHEEL_IDLE_MS))
  expect(onZoomX).toHaveBeenCalledTimes(1)
  expect(onZoomX.mock.calls[0][0]).toBeCloseTo(1.15 ** 4, 10)
  expect(onYRange).toHaveBeenCalledTimes(1)
  const { top, bottom } = onYRange.mock.calls[0][0]
  expect(top).toBeGreaterThan(bottom)
})

test('pan: jeden zápis pohledu na snímek, pásmo Y jednou na pointerup, zoom X ne', () => {
  const { overlay, onView, onZoomX, onYRange } = setup()
  fireEvent.pointerDown(overlay, { ...CENTER, pointerId: 1 })
  for (let step = 1; step <= 6; step += 1) {
    fireEvent.pointerMove(overlay, { clientX: 600 + step * 10, clientY: 300 + step * 5, pointerId: 1 }) // prettier-ignore
    if (step % 2 === 0) runFrame() // dva pohyby na snímek
  }
  expect(onView).toHaveBeenCalledTimes(3)
  expect(onView.mock.calls[2][0]).toMatchObject({ offsetX: 60, offsetY: 30 })
  expect(onYRange).not.toHaveBeenCalled()

  fireEvent.pointerUp(overlay, { clientX: 660, clientY: 330, pointerId: 1 })
  expect(onYRange).toHaveBeenCalledTimes(1)
  expect(onZoomX).not.toHaveBeenCalled()
})

test('pointerup dorovná krok, na který ještě nedošel snímek', () => {
  const { overlay, onView, onYRange } = setup()
  fireEvent.pointerDown(overlay, { ...CENTER, pointerId: 1 })
  fireEvent.pointerMove(overlay, { clientX: 620, clientY: 310, pointerId: 1 })
  fireEvent.pointerUp(overlay, { clientX: 620, clientY: 310, pointerId: 1 })
  expect(onView).toHaveBeenCalledTimes(1)
  expect(onView.mock.calls[0][0]).toMatchObject({ offsetX: 20, offsetY: 10 })
  expect(onYRange).toHaveBeenCalledTimes(1)
  // Zrušený snímek už nic nezapíše
  runFrame()
  expect(onView).toHaveBeenCalledTimes(1)
})

test('tažení osy X persistuje zoom X jednou na konci, pásmo Y ne', () => {
  const { overlay, onZoomX, onYRange } = setup()
  fireEvent.pointerDown(overlay, { ...X_AXIS, pointerId: 1 })
  for (let step = 1; step <= 5; step += 1) {
    fireEvent.pointerMove(overlay, { clientX: 600 - step * 20, clientY: 635, pointerId: 1 })
    runFrame()
  }
  expect(onZoomX).not.toHaveBeenCalled()
  fireEvent.pointerUp(overlay, { clientX: 500, clientY: 635, pointerId: 1 })
  expect(onZoomX).toHaveBeenCalledTimes(1)
  expect(onZoomX.mock.calls[0][0]).toBeCloseTo(Math.exp(100 * 0.005), 10)
  expect(onYRange).not.toHaveBeenCalled()
})

test('zrušený pointer ukončí tažení — pohyb bez tlačítka už graf neposouvá', () => {
  const { overlay, onView, onYRange } = setup()
  fireEvent.pointerDown(overlay, { ...CENTER, pointerId: 1 })
  fireEvent.pointerMove(overlay, { clientX: 620, clientY: 300, pointerId: 1 })
  runFrame()
  fireEvent.pointerCancel(overlay, { pointerId: 1 })
  expect(onYRange).not.toHaveBeenCalled() // pan jen po X: pásmo Y se nezměnilo
  onView.mockClear()
  fireEvent.pointerMove(overlay, { clientX: 700, clientY: 350, pointerId: 1 })
  runFrame()
  expect(onView).not.toHaveBeenCalled()
})

test('tažení po kolečku: gesto kolečka skončí a persistuje samo za sebe', () => {
  const { overlay, onZoomX } = setup()
  fireEvent.wheel(overlay, { deltaY: -100, ...CENTER })
  fireEvent.pointerDown(overlay, { ...CENTER, pointerId: 1 })
  expect(onZoomX).toHaveBeenCalledTimes(1)
  fireEvent.pointerMove(overlay, { clientX: 640, clientY: 300, pointerId: 1 })
  fireEvent.pointerUp(overlay, { clientX: 640, clientY: 300, pointerId: 1 })
  act(() => vi.advanceTimersByTime(WHEEL_IDLE_MS * 2))
  expect(onZoomX).toHaveBeenCalledTimes(1) // pan zoom nemění
})

test('změna datasetu zahodí rozběhnuté gesto bez persistence', () => {
  const { overlay, onZoomX, onYRange, rerender } = setup('ES')
  fireEvent.wheel(overlay, { deltaY: -100, ...CENTER })
  rerender('NQ')
  runFrame()
  act(() => vi.advanceTimersByTime(WHEEL_IDLE_MS * 2))
  expect(onZoomX).not.toHaveBeenCalled()
  expect(onYRange).not.toHaveBeenCalled()
})

test('reset dvojklikem zahodí rozběhnuté gesto kolečka', () => {
  const { overlay, onView, onZoomX } = setup()
  fireEvent.wheel(overlay, { deltaY: -100, ...CENTER })
  fireEvent.doubleClick(overlay, CENTER)
  runFrame()
  act(() => vi.advanceTimersByTime(WHEEL_IDLE_MS * 2))
  expect(onZoomX).not.toHaveBeenCalled()
  // Po resetu zůstává výchozí pohled, odložený krok kolečka ho nepřepíše
  expect(onView.mock.calls.at(-1)?.[0].zoomX).toBe(1)
})

test('měřič: krok gesta = jeden span gestureFrame a render Heatmapy', async () => {
  const meter = await import('../perf/meter')
  meter.resetPerf()
  meter.setPerfEnabled(true)
  try {
    const { overlay } = setup()
    const before = meter.perfCounters().renderHeatmap ?? 0
    fireEvent.wheel(overlay, { deltaY: -100, ...CENTER })
    fireEvent.wheel(overlay, { deltaY: -100, ...CENTER })
    runFrame()
    const counters = meter.perfCounters()
    expect(counters.gestureFrame).toBe(1)
    expect(counters.renderHeatmap - before).toBe(1)
  } finally {
    meter.setPerfEnabled(false)
    meter.resetPerf()
  }
})
