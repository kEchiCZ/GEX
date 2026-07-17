/** Testy playbacku (issue #27): krájení v paměti, rychlosti, live doraz, Arrow loader. */
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { tableFromArrays, tableToIPC } from 'apache-arrow'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { PlaybackBar } from '../components/PlaybackBar'
import { demoGrid } from '../heatmap/demo'
import { buildReplayDay } from './loader'
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
  const row = day.profileByMinute[0][0]
  expect(row.callOiChange).toBe(20)
  expect(row.putOiChange).toBe(-50)
  // Profil per minuta: combined komponenty s |delta| vahou
  expect(day.profileByMinute[1][0].callVolComponent).toBeCloseTo(30 * 0.5)
  expect(day.profileByMinute[1][0].putOiComponent).toBeCloseTo(200 * 0.4)
  expect(day.profileByMinute[1][0].distanceFromSpot).toBeCloseTo(7600 - 7601.5)
})
