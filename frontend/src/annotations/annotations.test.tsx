/** Testy anotací (issue #28): datové souřadnice, persistence, guma, reload. */
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { Heatmap } from '../components/Heatmap'
import { demoGrid } from '../heatmap/demo'
import { CrosshairProvider } from '../state/Crosshair'
import { nearestAnnotationId } from './model'
import { useAnnotations } from './useAnnotations'
import type { AnnotationPayload, StoredAnnotation } from './model'

const SAVED: StoredAnnotation = {
  id: 7,
  payload: {
    tool: 'line',
    color: '#ff0000',
    points: [
      { minute: 10, strike: 7420 },
      { minute: 30, strike: 7450 },
    ],
  },
}

function mockFetch(overrides: Partial<Record<string, unknown>> = {}) {
  const fetchMock = vi.fn(async (url: unknown, init?: RequestInit) => {
    const target = String(url)
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as { payload: AnnotationPayload }
      return {
        ok: true,
        json: async () => ({ id: 42, symbol: 'ES', day: '2026-07-16', payload: body.payload }),
      }
    }
    if (init?.method === 'DELETE') {
      return { ok: true, status: 204, json: async () => ({}) }
    }
    if (target.includes('/annotations')) {
      return { ok: true, json: async () => ({ annotations: [SAVED] }) }
    }
    return { ok: false, status: 404, json: async () => ({}) }
  })
  vi.stubGlobal('fetch', Object.assign(fetchMock, overrides))
  return fetchMock
}

beforeEach(() => vi.restoreAllMocks())

// ── nearestAnnotationId (guma) ─────────────────────────────────────

test('guma najde anotaci v toleranci, mimo toleranci nic', () => {
  expect(nearestAnnotationId([SAVED], { minute: 11, strike: 7421 }, 5, 10)).toBe(7)
  expect(nearestAnnotationId([SAVED], { minute: 200, strike: 7800 }, 5, 10)).toBeNull()
})

// ── useAnnotations: reload persistence (AC) ────────────────────────

test('anotace se načtou z API při mountu (přežijí reload)', async () => {
  const fetchMock = mockFetch()
  const { result } = renderHook(() => useAnnotations('ES', '2026-07-16'))

  await waitFor(() => expect(result.current.annotations).toHaveLength(1))
  expect(result.current.annotations[0]).toEqual(SAVED)
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining('/annotations?symbol=ES&date=2026-07-16'),
  )
})

test('create pošle POST s payloadem v čas×strike souřadnicích; erase pošle DELETE', async () => {
  const fetchMock = mockFetch()
  const { result } = renderHook(() => useAnnotations('ES', '2026-07-16'))
  await waitFor(() => expect(result.current.annotations).toHaveLength(1))

  const payload: AnnotationPayload = {
    tool: 'arrow',
    color: '#00ff00',
    points: [
      { minute: 5, strike: 7410 },
      { minute: 8, strike: 7435 },
    ],
  }
  await act(() => result.current.create(payload))
  expect(result.current.annotations).toHaveLength(2)
  const postCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
  expect(postCall).toBeDefined()
  const body = JSON.parse(String(postCall![1]!.body))
  expect(body.symbol).toBe('ES')
  expect(body.day).toBe('2026-07-16')
  expect(body.payload.points[0]).toEqual({ minute: 5, strike: 7410 }) // data, ne pixely

  await act(() => result.current.erase(7))
  expect(result.current.annotations.map((a) => a.id)).toEqual([42])
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => init?.method === 'DELETE' && String(url).endsWith('/annotations/7'),
    ),
  ).toBe(true)
})

// ── Kreslení na heatmapě: drag → payload v datových souřadnicích ──

test('tažení s nástrojem linie vytvoří anotaci vázanou na čas×strike (AC)', () => {
  const grid = demoGrid(100, 10) // canvas 1200×640 → buňka 12×64 px
  const created: AnnotationPayload[] = []
  render(
    <CrosshairProvider>
      <Heatmap
        grid={grid}
        style="gradient"
        contours="off"
        annotationTool="line"
        annotationColor="#123456"
        onAnnotationCreate={(payload) => created.push(payload)}
      />
    </CrosshairProvider>,
  )
  const overlay = screen.getByRole('img', { name: 'GEX heatmapa' })

  fireEvent.pointerDown(overlay, { clientX: 120, clientY: 320 })
  fireEvent.pointerMove(overlay, { clientX: 600, clientY: 64 })
  fireEvent.pointerUp(overlay)

  expect(created).toHaveLength(1)
  const { points, tool, color } = created[0]
  expect(tool).toBe('line')
  expect(color).toBe('#123456')
  expect(points).toHaveLength(2)
  // x=120 → minuta ~9.5; y=320 → řádek 5 shora → strike ~7420+; hodnoty v datových rozsazích
  expect(points[0].minute).toBeCloseTo(9.5, 1)
  expect(points[1].minute).toBeCloseTo(49.5, 1)
  expect(points[0].strike).toBeGreaterThan(grid.strikes[0])
  expect(points[0].strike).toBeLessThan(grid.strikes.at(-1)!)
  // y=64 → řádek 0.5 shora → interpolovaný strike mezi strikes[8] a strikes[9]
  expect(points[1].strike).toBeCloseTo(grid.strikes[8] + 2.5, 5)
  expect(points[1].strike).toBeGreaterThan(points[0].strike)
})

test('guma na heatmapě zavolá onAnnotationErase s id nejbližší anotace', () => {
  const grid = demoGrid(100, 10)
  const erased: number[] = []
  const annotation: StoredAnnotation = {
    id: 9,
    payload: {
      tool: 'line',
      color: '#fff',
      points: [
        { minute: 9.5, strike: grid.strikes[5] },
        { minute: 20, strike: grid.strikes[6] },
      ],
    },
  }
  render(
    <CrosshairProvider>
      <Heatmap
        grid={grid}
        style="gradient"
        contours="off"
        annotations={[annotation]}
        annotationTool="eraser"
        onAnnotationErase={(id) => erased.push(id)}
      />
    </CrosshairProvider>,
  )
  const overlay = screen.getByRole('img', { name: 'GEX heatmapa' })
  // Bod blízko prvního bodu anotace: minuta ~9.5 → x=120; strike[5] → řádek 4 shora → y ≈ 288
  fireEvent.pointerDown(overlay, { clientX: 120, clientY: 288 })
  expect(erased).toEqual([9])
})
