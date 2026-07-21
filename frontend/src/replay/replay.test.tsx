/** Testy playbacku (issue #27): krájení v paměti, rychlosti, live doraz, Arrow loader. */
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { tableFromArrays, tableToIPC } from 'apache-arrow'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { PlaybackBar } from '../components/PlaybackBar'
import { demoGrid } from '../heatmap/demo'
import { appendMinute, assembleReplayDay, buildReplayDay, decodeBundle } from './loader'
import type { LiveMinute, ReplayDay } from './loader'
import { sliceGrid, sliceOverlays, slicePanels, sliceSeries } from './slice'
import { usePlayback, TICK_MS } from './usePlayback'
import type { OverlayData } from '../heatmap/overlays'

// ── Krájení v paměti (AC: bez fetch per frame) ─────────────────────

test('sliceGrid vynuluje buňky po pozici, osy zůstávají', () => {
  const full = demoGrid(10, 4)
  const sliced = sliceGrid(full, 3)

  expect(sliced.minutes).toBe(10)
  expect(sliced.strikes).toEqual(full.strikes)
  const index = (strikeIdx: number, minuteIdx: number) => strikeIdx * 10 + minuteIdx
  expect(sliced.layers.call![index(2, 3)]).toBe(full.layers.call![index(2, 3)])
  expect(sliced.layers.call![index(2, 4)]).toBe(0) // po pozici prázdno
  expect(full.layers.call![index(2, 4)]).not.toBe(0) // původní data netknutá
})

test('sliceSeries a slicePanels drží délku (stabilní osa X)', () => {
  expect(sliceSeries([1, 2, 3, 4], 1)).toEqual([1, 2, 0, 0])
  const panels = slicePanels(
    {
      vol: [1, 2, 3],
      optVolCall: [1, 1, 1],
      optVolPut: [2, 2, 2],
      cumDelta: [5, -5, 9],
      deltaFlowCall: [1, 2, 3],
      deltaFlowPut: [3, 2, 1],
    },
    0,
  )
  expect(panels.vol).toEqual([1, 0, 0])
  expect(panels.cumDelta).toEqual([5, 0, 0])
})

test('sliceOverlays usekne cenu a levels po pozici', () => {
  const overlays: OverlayData = {
    price: [
      { minuteIdx: 0, close: 7600, up: true },
      { minuteIdx: 2, close: 7610, up: true },
    ],
    levels: [{ name: 'flip', color: '#fff', series: [7590, 7595, 7600] }],
    sessions: [
      { minuteIdx: 1, label: 'London' },
      { minuteIdx: 2, label: 'NY' },
    ],
  }
  const sliced = sliceOverlays(overlays, 1)
  expect(sliced.price).toHaveLength(1)
  expect(sliced.levels?.[0].series).toEqual([7590, 7595, null])
  expect(sliced.sessions).toHaveLength(1)
})

// ── usePlayback ────────────────────────────────────────────────────

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

test('start na live konci; play od pozice postupuje rychlostí; doraz = live', () => {
  const { result } = renderHook(() => usePlayback(100))
  expect(result.current.position).toBe(99)
  expect(result.current.isLive).toBe(true)

  act(() => result.current.seek(10))
  expect(result.current.isLive).toBe(false)

  act(() => result.current.setSpeed(5))
  act(() => result.current.play())
  act(() => vi.advanceTimersByTime(TICK_MS * 3))
  expect(result.current.position).toBe(25) // 10 + 3×5

  act(() => result.current.setSpeed(20))
  act(() => vi.advanceTimersByTime(TICK_MS * 4))
  expect(result.current.position).toBe(99) // doraz vpravo
  expect(result.current.isLive).toBe(true)
  expect(result.current.playing).toBe(false) // na dorazu se přehrávání zastaví
})

test('goLive vrací okamžitě na konec dne', () => {
  const { result } = renderHook(() => usePlayback(50))
  act(() => result.current.seek(5))
  act(() => result.current.goLive())
  expect(result.current.position).toBe(49)
  expect(result.current.isLive).toBe(true)
})

// ── PlaybackBar ────────────────────────────────────────────────────

function BarHarness() {
  const playback = usePlayback(100)
  return <PlaybackBar playback={playback} />
}

test('slider přetáčí, rychlosti se přepínají, live chip svítí na konci', () => {
  render(<BarHarness />)
  const slider = screen.getByLabelText('Pozice dne') as HTMLInputElement
  expect(slider.value).toBe('99')
  expect(screen.getByLabelText('Návrat na live').className).toContain('active')

  fireEvent.change(slider, { target: { value: '40' } })
  expect(slider.value).toBe('40')
  expect(screen.getByLabelText('Návrat na live').className).not.toContain('active')

  fireEvent.click(screen.getByRole('button', { name: '20×' }))
  expect(screen.getByRole('button', { name: '20×' }).className).toContain('active')

  fireEvent.click(screen.getByLabelText('Návrat na live'))
  expect(slider.value).toBe('99')
})

// ── Replay loader (Arrow round-trip) ───────────────────────────────

test('buildReplayDay dekóduje Arrow snapshoty a poskládá den', () => {
  const table = tableFromArrays({
    ts_min: [
      '2026-07-16T15:00:00Z',
      '2026-07-16T15:00:00Z',
      '2026-07-16T15:01:00Z',
      '2026-07-16T15:01:00Z',
    ],
    strike: Float64Array.from([7600, 7600, 7600, 7600]),
    right: ['C', 'P', 'C', 'P'],
    volume: Float64Array.from([10, 5, 30, 12]),
    oi: Float64Array.from([100, 200, 100, 200]),
    delta: Float64Array.from([0.5, -0.4, 0.5, -0.4]),
    stale_age: Float64Array.from([0, 0, 0, 0]),
  })
  const base64 = btoa(String.fromCharCode(...tableToIPC(table, 'stream')))

  const day = buildReplayDay({
    symbol: 'ES',
    expiry: '20260716',
    date: '2026-07-16',
    snapshots_arrow_base64: base64,
    levels: [
      {
        ts_min: '2026-07-16T15:00:00Z',
        flip: 7595.0,
        call_wall: 7650.0,
        put_wall: 7500.0,
        centroid: 7598.0,
      },
    ],
    flow: [
      { ts_min: '2026-07-16T15:00:00Z', flow_delta: 50, cum_delta: 50 },
      { ts_min: '2026-07-16T15:01:00Z', flow_delta: -20, cum_delta: 30 },
    ],
    bars: [
      { ts_min: '2026-07-16T15:00:00Z', close: 7600.5, volume: 1000 },
      { ts_min: '2026-07-16T15:01:00Z', close: 7601.5, volume: 1200 },
    ],
    oi_prev: [
      { strike: 7600, right: 'C', oi: 80 },
      { strike: 7600, right: 'P', oi: 250 },
    ],
  })

  expect(day.minutes).toHaveLength(2)
  expect(day.grid.strikes).toEqual([7600])
  // OI vrstvy normalizované p99 (max 200) → call 0.5, put 1.0
  expect(day.grid.layers.call![0]).toBeCloseTo(0.5)
  expect(day.grid.layers.put![0]).toBeCloseTo(1.0)
  // Panely: OptVol = kladný přírůstek volume (30-10=20 call, 12-5=7 put v minutě 1)
  expect(day.panels.optVolCall).toEqual([0, 20])
  expect(day.panels.optVolPut).toEqual([0, 7])
  expect(day.panels.cumDelta).toEqual([50, 30])
  expect(day.panels.vol).toEqual([1000, 1200])
  // Levels řada: minuta 0 hodnota, minuta 1 null
  expect(day.overlays.levels?.[0].series).toEqual([7595, null])
  // ΔOI vs. včera: dnešní OI (C 100, P 200) − včerejší (C 80, P 250)
  const row = day.profileByMinute.rowsAt(0)[0]
  expect(row.callOiChange).toBe(20)
  expect(row.putOiChange).toBe(-50)
  // Profil per minuta: combined komponenty s |delta| vahou
  expect(day.profileByMinute.rowsAt(1)[0].callVolComponent).toBeCloseTo(30 * 0.5)
  expect(day.profileByMinute.rowsAt(1)[0].putOiComponent).toBeCloseTo(200 * 0.4)
  expect(day.profileByMinute.rowsAt(1)[0].distanceFromSpot).toBeCloseTo(7600 - 7601.5)
})

// ── Inkrementální append (#127): append == plný build ───────────────

type Cell = {
  ts: string
  strike: number
  right: 'C' | 'P'
  volume: number
  oi: number
  delta: number
}

const M0 = '2026-07-16T15:00:00Z'
const M1 = '2026-07-16T15:01:00Z'
const CELLS: Cell[] = [
  { ts: M0, strike: 7600, right: 'C', volume: 10, oi: 100, delta: 0.5 },
  { ts: M0, strike: 7600, right: 'P', volume: 5, oi: 200, delta: -0.4 },
  { ts: M0, strike: 7610, right: 'C', volume: 8, oi: 80, delta: 0.4 },
  { ts: M0, strike: 7610, right: 'P', volume: 3, oi: 90, delta: -0.3 },
  { ts: M1, strike: 7600, right: 'C', volume: 30, oi: 100, delta: 0.5 },
  { ts: M1, strike: 7600, right: 'P', volume: 12, oi: 200, delta: -0.4 },
  { ts: M1, strike: 7610, right: 'C', volume: 20, oi: 80, delta: 0.45 },
  { ts: M1, strike: 7610, right: 'P', volume: 6, oi: 90, delta: -0.3 },
]
const BARS = [
  { ts_min: M0, open: 7600, high: 7601, low: 7599, close: 7600.5, volume: 1000 },
  { ts_min: M1, open: 7600.5, high: 7603, low: 7600, close: 7602, volume: 1300 },
]
const LEVELS = [
  { ts_min: M0, flip: 7595, centroid: 7598, call_wall: 7650, put_wall: 7500 },
  { ts_min: M1, flip: 7596, centroid: 7599, call_wall: 7655, put_wall: 7505 },
]
const FLOW = [
  { ts_min: M0, flow_delta: 50, cum_delta: 50 },
  { ts_min: M1, flow_delta: -20, cum_delta: 30 },
]

function bundleFor(cells: Cell[], bars: typeof BARS, levels: typeof LEVELS, flow: typeof FLOW) {
  const table = tableFromArrays({
    ts_min: cells.map((c) => c.ts),
    strike: Float64Array.from(cells.map((c) => c.strike)),
    right: cells.map((c) => c.right),
    volume: Float64Array.from(cells.map((c) => c.volume)),
    oi: Float64Array.from(cells.map((c) => c.oi)),
    delta: Float64Array.from(cells.map((c) => c.delta)),
    stale_age: Float64Array.from(cells.map(() => 0)),
  })
  return {
    symbol: 'ES',
    expiry: '20260716',
    date: '2026-07-16',
    snapshots_arrow_base64: btoa(String.fromCharCode(...tableToIPC(table, 'stream'))),
    levels,
    flow,
    bars,
  }
}

/** Porovnatelný tvar dne (typed arrays → obyčejná pole). */
function normalize(day: ReplayDay) {
  return {
    minutes: day.minutes,
    strikes: day.raw.strikes,
    call: Array.from(day.grid.layers.call ?? []),
    put: Array.from(day.grid.layers.put ?? []),
    signed: Array.from(day.grid.layers.signed ?? []),
    callOi: Array.from(day.raw.callOi),
    putOi: Array.from(day.raw.putOi),
    panels: day.panels,
    price: day.overlays.price,
    levels: day.overlays.levels,
    walls: day.overlays.walls,
    // Líný profil (#142) se pro porovnání zmaterializuje přes všechny minuty
    profile: Array.from({ length: day.profileByMinute.length }, (_, minuteIdx) =>
      day.profileByMinute.rowsAt(minuteIdx),
    ),
  }
}

test('appendMinute dá identický výsledek jako plný build (#127)', () => {
  const full = buildReplayDay(bundleFor(CELLS, BARS, LEVELS, FLOW))

  const firstMinute = decodeBundle(
    bundleFor(
      CELLS.filter((c) => c.ts === M0),
      [BARS[0]],
      [LEVELS[0]],
      [FLOW[0]],
    ),
  )
  const secondMinute: LiveMinute = {
    tsIso: M1,
    rows: CELLS.filter((c) => c.ts === M1).map((c) => ({
      strike: c.strike,
      right: c.right,
      oi: c.oi,
      volume: c.volume,
      delta: c.delta,
    })),
    bar: { open: 7600.5, high: 7603, low: 7600, close: 7602, volume: 1300 },
    levels: { flip: 7596, centroid: 7599, call_wall: 7655, put_wall: 7505 },
    flow: { cum_delta: 30 },
  }
  const incremental = assembleReplayDay(appendMinute(firstMinute, secondMinute))

  expect(normalize(incremental)).toEqual(normalize(full))
})

test('appendMinute přidá nový strike (posun osy) beze ztráty starých buněk (#127)', () => {
  const firstMinute = decodeBundle(
    bundleFor(
      CELLS.filter((c) => c.ts === M0),
      [BARS[0]],
      [LEVELS[0]],
      [FLOW[0]],
    ),
  )
  // Nová minuta přinese strike 7620 navíc → osa strikes se rozšíří
  const withNewStrike: LiveMinute = {
    tsIso: M1,
    rows: [
      { strike: 7600, right: 'C', oi: 100, volume: 30, delta: 0.5 },
      { strike: 7620, right: 'C', oi: 40, volume: 15, delta: 0.6 },
    ],
    bar: { close: 7602, volume: 1300 },
  }
  const inputs = appendMinute(firstMinute, withNewStrike)
  expect(inputs.strikes).toEqual([7600, 7610, 7620])
  expect(inputs.minutes).toHaveLength(2)
  const day = assembleReplayDay(inputs)
  // Stará buňka 7610 C v minutě 0 zůstala (strikeIdx 1, minuteIdx 0)
  const idx7610m0 = 1 * 2 + 0
  expect(day.raw.callOi[idx7610m0]).toBe(80)
  // Nový strike 7620 C v minutě 1 (strikeIdx 2, minuteIdx 1)
  expect(day.raw.callOi[2 * 2 + 1]).toBe(40)
})
