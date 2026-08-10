/** Testy anotací (issue #28): datové souřadnice, persistence, guma, reload. */
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { Heatmap } from '../components/Heatmap'
import { demoGrid } from '../heatmap/demo'
import { CrosshairProvider } from '../state/Crosshair'
import {
  axisIndexFromMinute,
  minuteAxisOffsets,
  minuteFromAxisIndex,
  nearestAnnotationId,
} from './model'
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
    if (init?.method === 'PUT') {
      const body = JSON.parse(String(init.body)) as { payload: AnnotationPayload }
      const id = Number(target.split('/').pop())
      return { ok: true, json: async () => ({ id, symbol: 'ES', day: '2026-07-16', payload: body.payload }) } // prettier-ignore
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

// ── Mapa 1m osy: minuta dne ↔ index sloupce (#502) ─────────────────

const isoMinute = (minute: number): string =>
  `2026-07-30T${String(14 + Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}:00.000Z`

test('minuteAxisOffsets: díra v ose se propíše do ofsetů; prázdná/nečitelná osa = null', () => {
  // Minuty 0–2, výpadek, pak od 48. minuty
  const holey = [0, 1, 2, 48, 49].map(isoMinute)
  expect(minuteAxisOffsets(holey)).toEqual([0, 1, 2, 48, 49])
  expect(minuteAxisOffsets([])).toBeNull()
  expect(minuteAxisOffsets(['not-a-date'])).toBeNull()
})

test('minuteFromAxisIndex/axisIndexFromMinute: interpolace přes díru a extrapolace za okraji', () => {
  const offsets = [0, 1, 2, 48, 49]
  // Uvnitř sloupce a přes díru (index 2.5 = půlka mezi minutami 2 a 48)
  expect(minuteFromAxisIndex(offsets, 3)).toBe(48)
  expect(minuteFromAxisIndex(offsets, 2.5)).toBeCloseTo(25)
  expect(axisIndexFromMinute(offsets, 48)).toBe(3)
  expect(axisIndexFromMinute(offsets, 25)).toBeCloseTo(2.5)
  // Za okraji 1 min / index (projekční zóna, minuty před začátkem)
  expect(minuteFromAxisIndex(offsets, 6)).toBe(51)
  expect(axisIndexFromMinute(offsets, 51)).toBe(6)
  expect(minuteFromAxisIndex(offsets, -1)).toBe(-1)
  expect(axisIndexFromMinute(offsets, -1)).toBe(-1)
  // Vzájemná inverze na souvislé ose = identita
  const contiguous = [0, 1, 2, 3]
  expect(minuteFromAxisIndex(contiguous, 1.25)).toBeCloseTo(1.25)
  expect(axisIndexFromMinute(contiguous, 1.25)).toBeCloseTo(1.25)
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

test('anotace se ukládají v absolutních minutách dne — TF bucket se převádí (#430)', () => {
  const grid = demoGrid(100, 10) // 100 bucketů × 5 min = 500 minut dne
  const created: AnnotationPayload[] = []
  render(
    <CrosshairProvider>
      <Heatmap
        grid={grid}
        style="gradient"
        contours="off"
        bucketMinutes={5}
        annotationTool="line"
        onAnnotationCreate={(payload) => created.push(payload)}
      />
    </CrosshairProvider>,
  )
  const overlay = screen.getByRole('img', { name: 'GEX heatmapa' })
  fireEvent.pointerDown(overlay, { clientX: 120, clientY: 320 })
  fireEvent.pointerMove(overlay, { clientX: 600, clientY: 64 })
  fireEvent.pointerUp(overlay)

  // Bucket ~9.5 × 5 min = minuta ~47.5 dne — nezávislé na zvoleném TF
  expect(created).toHaveLength(1)
  expect(created[0].points[0].minute).toBeCloseTo(47.5, 1)
  expect(created[0].points[1].minute).toBeCloseTo(247.5, 1)
})

test('guma najde anotaci v absolutních minutách i při jiné velikosti bucketu (#430)', () => {
  const grid = demoGrid(100, 10)
  const erased: number[] = []
  const annotation: StoredAnnotation = {
    id: 4,
    payload: {
      tool: 'line',
      color: '#fff',
      // Minuta 47.5 dne = bucket 9.5 na 5min gridu → x = 120 px
      points: [
        { minute: 47.5, strike: grid.strikes[5] },
        { minute: 100, strike: grid.strikes[6] },
      ],
    },
  }
  render(
    <CrosshairProvider>
      <Heatmap
        grid={grid}
        style="gradient"
        contours="off"
        bucketMinutes={5}
        annotations={[annotation]}
        annotationTool="eraser"
        onAnnotationErase={(id) => erased.push(id)}
      />
    </CrosshairProvider>,
  )
  const overlay = screen.getByRole('img', { name: 'GEX heatmapa' })
  fireEvent.pointerDown(overlay, { clientX: 120, clientY: 288 })
  expect(erased).toEqual([4])
})

test('anotace nad osou s dírou se uloží ve skutečné minutě dne, ne indexu (#502)', () => {
  // Osa 100 sloupců: minuty 0–2, výpadek sběru (45 min), pak souvisle od 48
  const holeyAxis = [0, 1, 2, ...Array.from({ length: 97 }, (_, i) => 48 + i)].map(isoMinute)
  const grid = demoGrid(100, 10) // canvas 1200 px → sloupec 12 px
  const created: AnnotationPayload[] = []
  render(
    <CrosshairProvider>
      <Heatmap
        grid={grid}
        style="gradient"
        contours="off"
        minutesIso={holeyAxis}
        annotationTool="line"
        onAnnotationCreate={(payload) => created.push(payload)}
      />
    </CrosshairProvider>,
  )
  const overlay = screen.getByRole('img', { name: 'GEX heatmapa' })
  // Sloupce 5 a 10 (x = 66 / 126 px) leží ZA dírou → minuty 50 a 55 dne
  fireEvent.pointerDown(overlay, { clientX: 66, clientY: 320 })
  fireEvent.pointerMove(overlay, { clientX: 126, clientY: 64 })
  fireEvent.pointerUp(overlay)

  expect(created).toHaveLength(1)
  expect(created[0].points[0].minute).toBeCloseTo(50, 1)
  expect(created[0].points[1].minute).toBeCloseTo(55, 1)
})

test('vložení minut doprostřed osy (backfill) anotaci neposune (#502)', () => {
  // Anotace ukotvená na minutách 50–55 dne (nakreslená nad osou s dírou);
  // rekonciliace díru dotáhne → osa má 145 souvislých minut
  const annotation: StoredAnnotation = {
    id: 3,
    payload: {
      tool: 'line',
      color: '#fff',
      points: [
        { minute: 50, strike: 7420 },
        { minute: 55, strike: 7425 },
      ],
    },
  }
  const backfilled = Array.from({ length: 145 }, (_, minute) => isoMinute(minute))
  const grid = demoGrid(145, 10) // sloupec 1200/145 px
  const erased: number[] = []
  render(
    <CrosshairProvider>
      <Heatmap
        grid={grid}
        style="gradient"
        contours="off"
        minutesIso={backfilled}
        annotations={[annotation]}
        annotationTool="eraser"
        onAnnotationErase={(id) => erased.push(id)}
      />
    </CrosshairProvider>,
  )
  const overlay = screen.getByRole('img', { name: 'GEX heatmapa' })
  const columnPx = 1200 / 145
  // Na pozici PŮVODNÍHO indexu 5 (kam by ji posunula indexová aritmetika) není nic
  fireEvent.pointerDown(overlay, { clientX: (5 + 0.5) * columnPx, clientY: 288 })
  expect(erased).toEqual([])
  // Na sloupci minuty 50 anotace je
  fireEvent.pointerDown(overlay, { clientX: (50 + 0.5) * columnPx, clientY: 288 })
  expect(erased).toEqual([3])
})

// ── Přesun tažením v režimu Kurzor (#589) ─────────────────────────

/** Anotace u sloupce 9.5 (x = 120 px) a řádku 5 zdola (y = 288 px) na demo gridu. */
function movable(grid: ReturnType<typeof demoGrid>): StoredAnnotation {
  return {
    id: 11,
    payload: {
      tool: 'line',
      color: '#abcdef',
      points: [
        { minute: 9.5, strike: grid.strikes[5] },
        { minute: 20, strike: grid.strikes[6] },
      ],
    },
  }
}

test('tažení anotace v režimu Kurzor ji přesune o rozdíl v datových souřadnicích (#589)', () => {
  const grid = demoGrid(100, 10) // sloupec 12 px, řádek 64 px
  const annotation = movable(grid)
  const moves: Array<{ id: number; payload: AnnotationPayload }> = []
  render(
    <CrosshairProvider>
      <Heatmap
        grid={grid}
        style="gradient"
        contours="off"
        annotations={[annotation]}
        annotationTool={null}
        onAnnotationMove={(id, payload) => moves.push({ id, payload })}
      />
    </CrosshairProvider>,
  )
  const overlay = screen.getByRole('img', { name: 'GEX heatmapa' })
  // Stisk na anotaci, tažení o 5 sloupců vpravo a o řádek nahoru
  fireEvent.pointerDown(overlay, { clientX: 120, clientY: 288 })
  fireEvent.pointerMove(overlay, { clientX: 180, clientY: 224 })
  fireEvent.pointerUp(overlay)

  expect(moves).toHaveLength(1)
  expect(moves[0].id).toBe(11)
  const { points, tool, color } = moves[0].payload
  expect(tool).toBe('line') // nástroj i barva zůstávají
  expect(color).toBe('#abcdef')
  // Oba body se posunuly o TENTÝŽ rozdíl (5 minut, 1 strike krok)
  const strikeStep = grid.strikes[1] - grid.strikes[0]
  expect(points[0].minute).toBeCloseTo(14.5, 5)
  expect(points[1].minute).toBeCloseTo(25, 5)
  expect(points[0].strike).toBeCloseTo(grid.strikes[5] + strikeStep, 5)
  expect(points[1].strike).toBeCloseTo(grid.strikes[6] + strikeStep, 5)
})

test('přesun se drží minut dne, takže na jiném TF posune o stejný čas (#589, #430)', () => {
  const grid = demoGrid(100, 10) // 5min koše → sloupec 12 px
  const annotation: StoredAnnotation = {
    id: 12,
    payload: {
      tool: 'arrow',
      color: '#fff',
      points: [
        { minute: 47.5, strike: grid.strikes[5] },
        { minute: 100, strike: grid.strikes[5] },
      ],
    },
  }
  const moves: Array<{ id: number; payload: AnnotationPayload }> = []
  render(
    <CrosshairProvider>
      <Heatmap
        grid={grid}
        style="gradient"
        contours="off"
        bucketMinutes={5}
        annotations={[annotation]}
        annotationTool={null}
        onAnnotationMove={(id, payload) => moves.push({ id, payload })}
      />
    </CrosshairProvider>,
  )
  const overlay = screen.getByRole('img', { name: 'GEX heatmapa' })
  fireEvent.pointerDown(overlay, { clientX: 120, clientY: 288 })
  fireEvent.pointerMove(overlay, { clientX: 156, clientY: 288 }) // 3 koše = 15 minut
  fireEvent.pointerUp(overlay)

  expect(moves).toHaveLength(1)
  expect(moves[0].payload.points[0].minute).toBeCloseTo(62.5, 5)
  expect(moves[0].payload.points[1].minute).toBeCloseTo(115, 5)
})

test('tažení mimo anotaci ji nepřesouvá (zůstává pan plochy) a klik bez tažení taky ne (#589)', () => {
  const grid = demoGrid(100, 10)
  const moves: number[] = []
  render(
    <CrosshairProvider>
      <Heatmap
        grid={grid}
        style="gradient"
        contours="off"
        annotations={[movable(grid)]}
        annotationTool={null}
        onAnnotationMove={(id) => moves.push(id)}
      />
    </CrosshairProvider>,
  )
  const overlay = screen.getByRole('img', { name: 'GEX heatmapa' })
  // Daleko od anotace → pan
  fireEvent.pointerDown(overlay, { clientX: 900, clientY: 100 })
  fireEvent.pointerMove(overlay, { clientX: 950, clientY: 150 })
  fireEvent.pointerUp(overlay)
  expect(moves).toEqual([])
  // Stisk na anotaci bez pohybu → žádné ukládání
  fireEvent.pointerDown(overlay, { clientX: 120, clientY: 288 })
  fireEvent.pointerUp(overlay)
  expect(moves).toEqual([])
})

test('move pošle PUT s novým payloadem a při chybě vrátí původní pozici (#589)', async () => {
  const fetchMock = mockFetch()
  const { result } = renderHook(() => useAnnotations('ES', '2026-07-16'))
  await waitFor(() => expect(result.current.annotations).toHaveLength(1))

  const moved: AnnotationPayload = {
    ...SAVED.payload,
    points: [
      { minute: 15, strike: 7430 },
      { minute: 35, strike: 7460 },
    ],
  }
  await act(() => result.current.move(7, moved))
  expect(result.current.annotations).toEqual([{ id: 7, payload: moved }])
  const putCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT')
  expect(putCall).toBeDefined()
  expect(String(putCall![0]).endsWith('/annotations/7')).toBe(true)
  expect(JSON.parse(String(putCall![1]!.body)).payload).toEqual(moved)

  // Selhání uložení musí vrátit původní pozici — jinak by po reloadu skočila zpět
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) })),
  )
  await act(() => result.current.move(7, SAVED.payload))
  expect(result.current.annotations).toEqual([{ id: 7, payload: moved }])
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
