/** Testy playbacku (issue #27): krájení v paměti, rychlosti, live doraz, Arrow loader. */
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { tableFromArrays, tableToIPC } from 'apache-arrow'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { BottomPanels } from '../components/BottomPanels'
import type { PanelSeries } from '../components/BottomPanels'
import { PlaybackBar } from '../components/PlaybackBar'
import { demoGrid } from '../heatmap/demo'
import { CrosshairProvider } from '../state/Crosshair'
import {
  appendMinute,
  assembleReplayDay,
  buildReplayDay,
  decodeBundle,
  oiTotalSeries,
} from './loader'
import type { LiveMinute, ReplayDay } from './loader'
import { accumulatePrintVol, outrightShareAt } from './loader'
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
    // Minuta 1 nese zmrzlou call kotaci (ADR-0015) — musí se dostat do profilu
    stale_age: Float64Array.from([0, 0, 54_000, 0]),
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
  // Stáří kotace se propisuje do profilu (ADR-0015) — panel z něj ztlumuje řádky.
  // Buňka bere maximum obou stran, takže zmrzlá call strana označí celý strike.
  expect(day.profileByMinute.rowsAt(0)[0].staleAge).toBe(0)
  expect(day.profileByMinute.rowsAt(1)[0].staleAge).toBe(54_000)
})

test('FA zdroj (#232): oiest přepisuje odhad, měřený režim je bit-identický', () => {
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
  const common = {
    symbol: 'ES',
    expiry: '20260716',
    date: '2026-07-16',
    snapshots_arrow_base64: btoa(String.fromCharCode(...tableToIPC(table, 'stream'))),
    levels: [],
    flow: [],
    bars: [],
  }
  const withoutFa = decodeBundle({ ...common })
  const withFa = decodeBundle({
    ...common,
    oiest: [{ ts_min: '2026-07-16T15:01:00Z', strike: 7600, right: 'C', oi_est: 140 }],
    gexprofilefa: [
      { ts_min: '2026-07-16T15:00:00Z', grid_start: 7590, grid_step: 5, values: [1, 2] },
    ],
  })

  // REGRES: měřené matice jsou bit-identické s během bez FA řad
  expect([...withFa.callOi]).toEqual([...withoutFa.callOi])
  expect([...withFa.putOi]).toEqual([...withoutFa.putOi])
  // Odhad: přepsaná jen buňka z oiest (index = strikeIdx·minutes + minuteIdx)
  expect(withFa.hasOiEst).toBe(true)
  expect(withoutFa.hasOiEst).toBe(false)
  expect(withFa.callOiEst[1]).toBe(140)
  expect(withFa.callOiEst[0]).toBe(withFa.callOi[0])
  expect([...withFa.putOiEst]).toEqual([...withFa.putOi])

  const day = assembleReplayDay(withFa)
  const dayWithout = assembleReplayDay(withoutFa)
  expect(dayWithout.rawFa).toBeNull()
  expect(day.rawFa).not.toBeNull()
  expect(day.rawFa!.callOi[1]).toBe(140)
  expect(day.raw.callOi[1]).toBe(100) // měřená vrstva odhad nevidí
  // Měřený grid (výchozí OI mód) se FA řadou nemění — bit-identita
  expect([...day.grid.layers.call!]).toEqual([...dayWithout.grid.layers.call!])
  expect([...day.grid.layers.put!]).toEqual([...dayWithout.grid.layers.put!])
  // FA Dyn GEX profil jede vedle měřeného
  expect(day.gexProfileFa?.[0]?.values).toEqual([1, 2])
  expect(day.gexProfile[0]).toBeNull()

  // Živý append: oiest minuty přepíše odhad, měření nechá
  const appended = appendMinute(withFa, {
    tsIso: '2026-07-16T15:02:00Z',
    rows: [
      { strike: 7600, right: 'C', oi: 100, volume: 40, delta: 0.5 },
      { strike: 7600, right: 'P', oi: 200, volume: 20, delta: -0.4 },
    ],
    oiEst: [{ strike: 7600, right: 'C', oi_est: 150 }],
  })
  expect(appended.callOi[2]).toBe(100)
  expect(appended.callOiEst[2]).toBe(150)
  expect(appended.putOiEst[2]).toBe(200) // bez odhadu = měření
  expect(appended.callOiEst[1]).toBe(140) // starší odhad přenos přežil
})

test('walldom řada: slabé úseky zdi + dominance v cenovce (ADR-0010, #223)', () => {
  const table = tableFromArrays({
    ts_min: ['2026-07-16T15:00:00Z', '2026-07-16T15:01:00Z'],
    strike: Float64Array.from([7600, 7600]),
    right: ['C', 'C'],
    volume: Float64Array.from([10, 20]),
    oi: Float64Array.from([100, 100]),
    delta: Float64Array.from([0.5, 0.5]),
    stale_age: Float64Array.from([0, 0]),
  })
  const day = buildReplayDay({
    symbol: 'ES',
    expiry: '20260716',
    date: '2026-07-16',
    snapshots_arrow_base64: btoa(String.fromCharCode(...tableToIPC(table, 'stream'))),
    levels: [
      { ts_min: '2026-07-16T15:00:00Z', call_wall: 7650, put_wall: 7500 },
      { ts_min: '2026-07-16T15:01:00Z', call_wall: 7650, put_wall: 7500 },
    ],
    walldom: [
      { ts_min: '2026-07-16T15:00:00Z', call_wall_dom: 0.08, put_wall_dom: 0.5 },
      { ts_min: '2026-07-16T15:01:00Z', call_wall_dom: 0.34, put_wall_dom: 0.5 },
    ],
    flow: [],
    bars: [],
  })

  const callWall = day.overlays.walls?.find((line) => line.name === 'call_wall')
  // Minuta 0 pod prahem 0.15 → slabý úsek; minuta 1 nad prahem
  expect(callWall?.weak).toEqual([true, false])
  // Cenovka nese poslední dominanci v %
  expect(callWall?.labelSuffix).toBe(' · 34 %')
  const putWall = day.overlays.walls?.find((line) => line.name === 'put_wall')
  expect(putWall?.weak).toEqual([false, false])
  expect(putWall?.labelSuffix).toBe(' · 50 %')
})

test('levelsfa řada: fa_* linie čárkovaně vedle měřených (ADR-0011, #222)', () => {
  const table = tableFromArrays({
    ts_min: ['2026-07-16T15:00:00Z'],
    strike: Float64Array.from([7600]),
    right: ['C'],
    volume: Float64Array.from([10]),
    oi: Float64Array.from([100]),
    delta: Float64Array.from([0.5]),
    stale_age: Float64Array.from([0]),
  })
  const day = buildReplayDay({
    symbol: 'ES',
    expiry: '20260716',
    date: '2026-07-16',
    snapshots_arrow_base64: btoa(String.fromCharCode(...tableToIPC(table, 'stream'))),
    levels: [{ ts_min: '2026-07-16T15:00:00Z', flip: 7595, call_wall: 7650, put_wall: 7500 }],
    levelsfa: [{ ts_min: '2026-07-16T15:00:00Z', flip: 7580, call_wall: 7640, put_wall: 7510 }],
    flow: [],
    bars: [],
  })
  const faFlip = day.overlays.levels?.find((line) => line.name === 'fa_flip')
  expect(faFlip?.series).toEqual([7580])
  expect(faFlip?.dash).toBeDefined() // odhad se kreslí čárkovaně
  expect(day.overlays.levels?.find((line) => line.name === 'fa_call_wall')?.series).toEqual([7640])
  expect(day.overlays.levels?.find((line) => line.name === 'flip')?.series).toEqual([7595])
})

test('ladder řada: žebřík per minuta z bundle (#244)', () => {
  const table = tableFromArrays({
    ts_min: ['2026-07-16T15:00:00Z', '2026-07-16T15:01:00Z'],
    strike: Float64Array.from([7600, 7600]),
    right: ['C', 'C'],
    volume: Float64Array.from([10, 20]),
    oi: Float64Array.from([100, 100]),
    delta: Float64Array.from([0.5, 0.5]),
    stale_age: Float64Array.from([0, 0]),
  })
  const day = buildReplayDay({
    symbol: 'ES',
    expiry: '20260716',
    date: '2026-07-16',
    snapshots_arrow_base64: btoa(String.fromCharCode(...tableToIPC(table, 'stream'))),
    levels: [],
    ladder: [
      {
        ts_min: '2026-07-16T15:01:00Z',
        call_strikes: [7650, 7700],
        call_shares: [0.4, 0.2],
        put_strikes: [7500],
        put_shares: [0.55],
      },
    ],
    flow: [],
    bars: [],
  })
  expect(day.ladder[0]).toBeNull() // minuta bez žebříku
  expect(day.ladder[1]).toMatchObject({
    callStrikes: [7650, 7700],
    callShares: [0.4, 0.2],
    putStrikes: [7500],
    putShares: [0.55],
  })
})

test('bez walldom řady (starší API) zůstávají zdi bez slabých úseků', () => {
  const table = tableFromArrays({
    ts_min: ['2026-07-16T15:00:00Z'],
    strike: Float64Array.from([7600]),
    right: ['C'],
    volume: Float64Array.from([10]),
    oi: Float64Array.from([100]),
    delta: Float64Array.from([0.5]),
    stale_age: Float64Array.from([0]),
  })
  const day = buildReplayDay({
    symbol: 'ES',
    expiry: '20260716',
    date: '2026-07-16',
    snapshots_arrow_base64: btoa(String.fromCharCode(...tableToIPC(table, 'stream'))),
    levels: [{ ts_min: '2026-07-16T15:00:00Z', call_wall: 7650 }],
    flow: [],
    bars: [],
  })
  const callWall = day.overlays.walls?.find((line) => line.name === 'call_wall')
  expect(callWall?.weak).toEqual([null]) // dominance neznámá → plný styl
  expect(callWall?.labelSuffix).toBeUndefined()
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
    provisionalMinutes: day.provisionalMinutes,
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

test('striky bez OI se dekódují do profilu jako chybějící, ne jako nula (#465)', () => {
  const bundle = {
    ...bundleFor(CELLS, BARS, LEVELS, FLOW),
    oimissing: [{ ts_min: M1, strike: 7610, right: 'C' }],
  }
  const day = buildReplayDay(bundle)

  const minute1 = day.profileByMinute.rowsAt(1)
  const strike7610 = minute1.find((row) => row.strike === 7610)!
  expect(strike7610.callOiMissing).toBe(true)
  expect(strike7610.putOiMissing).toBe(false) // put stranu nikdo neoznačil

  // Jiná minuta téhož striku je změřená — příznak je per minuta, ne per strike
  expect(day.profileByMinute.rowsAt(0).find((row) => row.strike === 7610)!.callOiMissing).toBe(
    false,
  )
  // Ostatní striky zůstávají beze změny
  expect(minute1.find((row) => row.strike === 7600)!.callOiMissing).toBe(false)
})

test('bez klíče oimissing (starší API) není nic označené (#465)', () => {
  const day = buildReplayDay(bundleFor(CELLS, BARS, LEVELS, FLOW))

  expect(
    day.profileByMinute.rowsAt(1).every((row) => !row.callOiMissing && !row.putOiMissing),
  ).toBe(true)
})

// ── Flip na řídké OI páteři + tasty fill (#664) ─

test('minuta s nadpoloviční dírou v OI ztlumí flip a označí cenovku (#664)', () => {
  // M1: 3 ze 4 kontraktů bez OI (75 % > práh 50 %) → řídká; M0 plná
  const bundle = {
    ...bundleFor(CELLS, BARS, LEVELS, FLOW),
    oimissing: [
      { ts_min: M1, strike: 7600, right: 'C' },
      { ts_min: M1, strike: 7600, right: 'P' },
      { ts_min: M1, strike: 7610, right: 'C' },
    ],
  }
  const day = buildReplayDay(bundle)

  const flip = day.overlays.levels!.find((line) => line.name === 'flip')!
  expect(flip.weak).toEqual([false, true])
  // Poslední měřená minuta je řídká → cenovka nese varování
  expect(flip.labelSuffix).toContain('OI')
})

test('plné OI: flip bez ztlumení i varování (#664)', () => {
  const day = buildReplayDay(bundleFor(CELLS, BARS, LEVELS, FLOW))

  const flip = day.overlays.levels!.find((line) => line.name === 'flip')!
  expect(flip.weak).toEqual([false, false])
  expect(flip.labelSuffix).toBeUndefined()
})

test('řada oifilled se dekóduje do vlastní množiny (#664)', () => {
  const bundle = {
    ...bundleFor(CELLS, BARS, LEVELS, FLOW),
    oifilled: [{ ts_min: M1, strike: 7610, right: 'C' }],
  }
  const inputs = decodeBundle(bundle)

  expect(inputs.oiFilled.has('2026-07-16T15:01:00.000Z|7610|C')).toBe(true)
  // Doplněná hodnota je měřená — do řídkosti se nepočítá
  expect(inputs.oiLowMinutes).toEqual([false, false])
})

// ── Catch-up minuta po startu enginu uprostřed dne (#518, ADR-0024) ─

test('catch-up minuta neprodukuje skokový přírůstek (#518)', () => {
  // Restart enginu: M0 je poslední minuta před výpadkem, M1 první sweep po
  // startu — jeho kumulativy dohánějí celou dobu výpadku
  const bundle = { ...bundleFor(CELLS, BARS, LEVELS, FLOW), catchup: [{ ts_min: M1 }] }
  const day = buildReplayDay(bundle)

  // Bez flagu by M1 dostala přírůstek 32 call / 10 put (kladné diffy kumulativů
  // obou striků) — s ním je první měřenou minutou dne a skok se nekreslí jako obchod
  expect(day.panels.optVolCall).toEqual([0, 0])
  expect(day.panels.optVolPut).toEqual([0, 0])
  expect(day.panels.deltaFlowCall).toEqual([0, 0])
  expect(day.panels.deltaFlowPut).toEqual([0, 0])
  // CumΔ řadu flag nemění — tu počítá engine sám od svého startu
  expect(day.panels.cumDelta).toEqual([50, 30])
})

test('minuta PO catch-up už počítá přírůstek proti ní (#518)', () => {
  // Start uprostřed dne: M0 je catch-up (první měřená minuta), M1 běžná —
  // kumulativy catch-up minuty už jsou správné, diff proti nim je poctivý
  const bundle = { ...bundleFor(CELLS, BARS, LEVELS, FLOW), catchup: [{ ts_min: M0 }] }
  const day = buildReplayDay(bundle)

  expect(day.panels.optVolCall).toEqual([0, 20 + 12])
  expect(day.panels.optVolPut).toEqual([0, 7 + 3])
})

test('CumΔ nese „od HH:MM", když den nezačíná od začátku seance (#518)', () => {
  // První měřená minuta dne je catch-up → měření začalo až startem enginu
  const withCatch = buildReplayDay({
    ...bundleFor(CELLS, BARS, LEVELS, FLOW),
    catchup: [{ ts_min: M0 }],
  })
  expect(withCatch.panels.cumDeltaFromIso).toBe(M0.replace('Z', '.000Z'))

  // Restart uprostřed dne (den začal normálně) ani běžný den popisek nemají
  const restarted = buildReplayDay({
    ...bundleFor(CELLS, BARS, LEVELS, FLOW),
    catchup: [{ ts_min: M1 }],
  })
  expect(restarted.panels.cumDeltaFromIso).toBeNull()
  expect(buildReplayDay(bundleFor(CELLS, BARS, LEVELS, FLOW)).panels.cumDeltaFromIso).toBeNull()
})

test('panel Cum Δ zobrazí popisek startu měření, bez něj nic (#518)', () => {
  const series: PanelSeries = {
    vol: [0, 0],
    optVolCall: [0, 0],
    optVolPut: [0, 0],
    cumDelta: [5, 9],
    deltaFlowCall: [0, 0],
    deltaFlowPut: [0, 0],
    cumDeltaFromIso: M0,
  }
  const visible = {
    vol: false,
    optVol: false,
    delta: true,
    deltaFlow: false,
    evoOi: false,
    sentiment: false,
  }
  const { unmount } = render(
    <CrosshairProvider>
      <BottomPanels data={series} visible={visible} />
    </CrosshairProvider>,
  )
  expect(screen.getByTestId('cumdelta-from').textContent).toContain('· od ')
  unmount()

  render(
    <CrosshairProvider>
      <BottomPanels data={{ ...series, cumDeltaFromIso: null }} visible={visible} />
    </CrosshairProvider>,
  )
  expect(screen.queryByTestId('cumdelta-from')).toBeNull()
})

test('živá catch-up minuta z WS neprodukuje špic (#518)', () => {
  const start = decodeBundle(
    bundleFor(
      CELLS.filter((c) => c.ts === M0),
      [BARS[0]],
      [LEVELS[0]],
      [FLOW[0]],
    ),
  )
  const catchUpMinute: LiveMinute = {
    tsIso: M1,
    catchUp: true, // aditivní klíč snapshot kanálu po restartu enginu
    rows: CELLS.filter((c) => c.ts === M1).map((c) => ({
      strike: c.strike,
      right: c.right,
      oi: c.oi,
      volume: c.volume,
      delta: c.delta,
    })),
    flow: { cum_delta: 30 },
  }
  const day = assembleReplayDay(appendMinute(start, catchUpMinute))

  expect(day.panels.optVolCall).toEqual([0, 0])
  expect(day.panels.deltaFlowPut).toEqual([0, 0])
})

// ── Díra ve sběru: osa X ze sjednocení snapshotů a barů (#459) ─────

/** M1 = minuta, kdy sweep neproběhl: bar backfill dotáhl, snapshot chybí. */
const MID = '2026-07-16T15:02:00Z'
const CELLS_WITH_GAP: Cell[] = [
  ...CELLS.filter((c) => c.ts === M0),
  // M1 chybí — sweep neproběhl; snapshot je až o minutu později
  { ts: MID, strike: 7600, right: 'C', volume: 30, oi: 100, delta: 0.5 },
  { ts: MID, strike: 7600, right: 'P', volume: 12, oi: 200, delta: -0.4 },
  { ts: MID, strike: 7610, right: 'C', volume: 20, oi: 80, delta: 0.45 },
  { ts: MID, strike: 7610, right: 'P', volume: 6, oi: 90, delta: -0.3 },
]
const BARS_WITH_GAP = [
  ...BARS,
  { ts_min: MID, open: 7602, high: 7605, low: 7601, close: 7604, volume: 900 },
]
// Engine v díře nezapsal ani levels, ani flow
const LEVELS_WITH_GAP = [
  LEVELS[0],
  { ts_min: MID, flip: 7596, centroid: 7599, call_wall: 7655, put_wall: 7505 },
]
const FLOW_WITH_GAP = [FLOW[0], { ts_min: MID, flow_delta: -20, cum_delta: 30 }]

test('backfillovaná svíčka bez snapshotu dostane vlastní sloupec osy (#459)', () => {
  const day = buildReplayDay(
    bundleFor(CELLS_WITH_GAP, BARS_WITH_GAP, LEVELS_WITH_GAP, FLOW_WITH_GAP),
  )

  // Osa nese i minutu, kde máme jen cenu — bez toho by svíčky navazovaly
  // a 1minutová díra by vypadala jako spojitý průběh
  expect(day.minutes).toEqual([M0, M1, MID].map((ts) => ts.replace('Z', '.000Z')))
  expect(day.overlays.price!.map((bar) => bar.minuteIdx)).toEqual([0, 1, 2])
  expect(day.overlays.price!.map((bar) => bar.close)).toEqual([7600.5, 7602, 7604])
})

test('sloupec bez snapshotu se nevydává za měření (#459)', () => {
  const day = buildReplayDay(
    bundleFor(CELLS_WITH_GAP, BARS_WITH_GAP, LEVELS_WITH_GAP, FLOW_WITH_GAP),
  )
  const gapIdx = 1

  // Profil z nul by tvrdil, že v celém řetězu není žádné OI
  expect(day.profileByMinute.rowsAt(gapIdx)).toEqual([])
  // Max Pain z nulového OI ukáže libovolný strike → v díře se nekreslí
  const maxPain = day.overlays.levels!.find((line) => line.name === 'max_pain')!
  expect(maxPain.series[gapIdx]).toBeNull()
  // Levels tam engine nezapsal → přerušení linie, ne dokreslený soused
  const flip = day.overlays.levels!.find((line) => line.name === 'flip')!
  expect(flip.series[gapIdx]).toBeNull()
})

test('přírůstkové panely počítají přes díru, ne vůči nulovému sloupci (#459)', () => {
  const day = buildReplayDay(
    bundleFor(CELLS_WITH_GAP, BARS_WITH_GAP, LEVELS_WITH_GAP, FLOW_WITH_GAP),
  )

  // Bez přeskočení díry by OptVol v MID vyskočil na celé kumulativní volume
  // (30+20 = 50 místo přírůstku 22 proti M0) — falešný špic po každém výpadku
  expect(day.panels.optVolCall[1]).toBe(0) // díra sama nic nenaměřila
  expect(day.panels.optVolCall[2]).toBeCloseTo(30 - 10 + (20 - 8), 5)
  expect(day.panels.deltaFlowCall[1]).toBe(0)
  expect(day.panels.deltaFlowCall[2]).toBeCloseTo((30 - 10) * 0.5 + (20 - 8) * 0.45, 5)
  // CumΔ je kumulativní: v díře drží poslední známou hodnotu, nepropadá na nulu
  expect(day.panels.cumDelta).toEqual([50, 50, 30])
})

test('append vsune minutu doprostřed osy a dá identický den jako plný build (#459)', () => {
  // Bar dorazí dřív než snapshot (#135), takže bar-only minuta vznikne v ose
  // a teprve pak přijde její snapshot — nová minuta se musí zařadit podle času
  const full = buildReplayDay(
    bundleFor(CELLS_WITH_GAP, BARS_WITH_GAP, LEVELS_WITH_GAP, FLOW_WITH_GAP),
  )

  const start = decodeBundle(
    bundleFor(
      CELLS_WITH_GAP.filter((c) => c.ts === M0),
      [BARS[0], BARS_WITH_GAP[2]], // M0 + MID: minuta M1 v ose ještě chybí
      [LEVELS[0]],
      [FLOW[0]],
    ),
  )
  expect(start.minutes).toEqual([M0, MID].map((ts) => ts.replace('Z', '.000Z')))

  // Dozadu doplněná minuta M1 (jen bar) — patří mezi M0 a MID
  const gapMinute: LiveMinute = {
    tsIso: M1,
    rows: [], // sweep neproběhl — jen cena z backfillu
    bar: { open: 7600.5, high: 7603, low: 7600, close: 7602, volume: 1300 },
  }
  const withGap = appendMinute(start, gapMinute)
  expect(withGap.minutes).toEqual([M0, M1, MID].map((ts) => ts.replace('Z', '.000Z')))
  // MID zatím zná jen svůj bar — snapshot dorazí až dalším WS příchodem
  expect(withGap.snapshotMinutes).toEqual([true, false, false])

  // Snapshot MID se nesmí přenosem buněk posunout na cizí sloupec
  const midSnapshot: LiveMinute = {
    tsIso: MID,
    rows: CELLS_WITH_GAP.filter((c) => c.ts === MID).map((c) => ({
      strike: c.strike,
      right: c.right,
      oi: c.oi,
      volume: c.volume,
      delta: c.delta,
    })),
    bar: { open: 7602, high: 7605, low: 7601, close: 7604, volume: 900 },
    levels: { flip: 7596, centroid: 7599, call_wall: 7655, put_wall: 7505 },
    flow: { cum_delta: 30 },
  }
  const incremental = assembleReplayDay(appendMinute(withGap, midSnapshot))

  expect(normalize(incremental)).toEqual(normalize(full))
})

test('bar aktuální wall-clock minuty je po dekódování bundle provizorní (#158)', () => {
  const bundle = bundleFor(CELLS, BARS, LEVELS, FLOW)
  // Reload uprostřed minuty M1: bar M1 je s jistotou rozdělaný → provizorní,
  // spot svíčka ho smí přebíjet; starší minuty zůstávají finální
  const during = decodeBundle(bundle, new Date('2026-07-16T15:01:31Z'))
  expect(during.bars.map((bar) => bar.final)).toEqual([true, false])
  const day = assembleReplayDay(during)
  expect(day.provisionalMinutes).toEqual([1])

  // Reload po uzavření minuty (jiná wall-clock minuta) → všechny bary finální
  const after = decodeBundle(bundle, new Date('2026-07-16T15:02:10Z'))
  expect(after.bars.map((bar) => bar.final)).toEqual([true, true])
  expect(assembleReplayDay(after).provisionalMinutes).toEqual([])
})

test('append == plný build přes 120 minut s rozšiřující se strike osou (#157)', () => {
  // Dlouhá sekvence z review #132: strike osa náhodně roste oběma směry,
  // řezy mají díry — append musí dát identický den jako plný build,
  // včetně panels, profilu a provisionalMinutes.
  let seed = 0x132132
  const random = () => {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff
    return seed / 0x7fffffff
  }
  const minutes = 120
  const isoOf = (minuteIdx: number) =>
    new Date(Date.UTC(2026, 6, 16, 13, 30 + minuteIdx)).toISOString()
  // btoa přes spread by na velkém Arrow IPC přetekl stack — skládat po chunkách
  const base64Of = (bytes: Uint8Array): string => {
    let binary = ''
    for (let index = 0; index < bytes.length; index += 8192) {
      binary += String.fromCharCode(...bytes.subarray(index, index + 8192))
    }
    return btoa(binary)
  }

  let low = 7600
  let high = 7620
  const allCells: Cell[] = []
  const perMinute: Array<{ ts: string; rows: Cell[] }> = []
  const bars: typeof BARS = []
  const levels: typeof LEVELS = []
  const flow: typeof FLOW = []
  let cumDelta = 0
  for (let minuteIdx = 0; minuteIdx < minutes; minuteIdx += 1) {
    const ts = isoOf(minuteIdx)
    if (random() < 0.15) low -= 5
    if (random() < 0.15) high += 5
    const rows: Cell[] = []
    for (let strike = low; strike <= high; strike += 5) {
      for (const right of ['C', 'P'] as const) {
        if (random() < 0.1) continue // díra v řezu
        rows.push({
          ts,
          strike,
          right,
          volume: Math.floor(random() * 500),
          oi: Math.floor(random() * 10000),
          delta: (right === 'C' ? 1 : -1) * random(),
        })
      }
    }
    const close = 7610 + Math.round((random() - 0.5) * 20)
    cumDelta += Math.round((random() - 0.5) * 100)
    allCells.push(...rows)
    bars.push({ ts_min: ts, open: close - 1, high: close + 2, low: close - 3, close, volume: 100 + minuteIdx }) // prettier-ignore
    levels.push({ ts_min: ts, flip: 7600, centroid: 7605, call_wall: high, put_wall: low })
    flow.push({ ts_min: ts, flow_delta: 0, cum_delta: cumDelta })
    perMinute.push({ ts, rows })
  }

  const bundleOf = (cells: Cell[], b: typeof BARS, l: typeof LEVELS, f: typeof FLOW) => {
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
      snapshots_arrow_base64: base64Of(tableToIPC(table, 'stream')),
      levels: l,
      flow: f,
      bars: b,
    }
  }

  const full = buildReplayDay(bundleOf(allCells, bars, levels, flow))

  let inputs = decodeBundle(bundleOf(perMinute[0].rows, [bars[0]], [levels[0]], [flow[0]]))
  for (let minuteIdx = 1; minuteIdx < minutes; minuteIdx += 1) {
    const bar = bars[minuteIdx]
    inputs = appendMinute(inputs, {
      tsIso: perMinute[minuteIdx].ts,
      rows: perMinute[minuteIdx].rows.map((c) => ({
        strike: c.strike,
        right: c.right,
        oi: c.oi,
        volume: c.volume,
        delta: c.delta,
      })),
      bar: { open: bar.open, high: bar.high, low: bar.low, close: bar.close, volume: bar.volume, final: true }, // prettier-ignore
      levels: levels[minuteIdx] as unknown as Record<string, number | null>,
      flow: { cum_delta: flow[minuteIdx].cum_delta },
    })
  }
  const incremental = assembleReplayDay(inputs)
  expect(normalize(incremental)).toEqual(normalize(full))
})

test('Dyn GEX profil: decode z bundle + WS append per minuta (ADR-0009)', () => {
  const withProfile = {
    ...bundleFor(CELLS, BARS, LEVELS, FLOW),
    gexprofile: [{ ts_min: M0, grid_start: 7600, grid_step: 5, values: [10.5, -3.5] }],
  }
  const inputs = decodeBundle(withProfile)
  expect(inputs.gexProfile).toHaveLength(1)
  const day = assembleReplayDay(inputs)
  expect(day.gexProfile[0]).toMatchObject({ gridStart: 7600, gridStep: 5, values: [10.5, -3.5] })
  expect(day.gexProfile[1]).toBeNull() // druhá minuta profil nemá

  // WS append doplní profil druhé minuty (upsert dle tsIso)
  const appended = appendMinute(inputs, {
    tsIso: M1,
    rows: [],
    gexProfile: { grid_start: 7595, grid_step: 5, values: [1, 2, 3] },
  })
  const day2 = assembleReplayDay(appended)
  expect(day2.gexProfile[1]).toMatchObject({ gridStart: 7595, values: [1, 2, 3] })

  // Starší API bez klíče gexprofile → prázdné pole, nic nepadá
  const legacy = assembleReplayDay(decodeBundle(bundleFor(CELLS, BARS, LEVELS, FLOW)))
  expect(legacy.gexProfile.every((row) => row === null)).toBe(true)
})

test('Dyn GEX pole: decode z bundle + WS nahrazuje starší stav (ADR-0009 fáze 2)', () => {
  const withField = {
    ...bundleFor(CELLS, BARS, LEVELS, FLOW),
    gexfield: [
      {
        ts_min: M0,
        grid_start: 7600,
        grid_step: 5,
        col_start: M1,
        col_step_min: 10,
        col_count: 2,
        values: [1, 2, 3, 4], // 2 sloupce × mřížka délky 2
      },
    ],
  }
  const day = assembleReplayDay(decodeBundle(withField))
  expect(day.gexField).toMatchObject({
    gridStart: 7600,
    colStepMin: 10,
    colCount: 2,
    values: [1, 2, 3, 4],
  })

  // WS append: nová minuta pole prostě nahradí (drží se jen poslední stav)
  const appended = appendMinute(decodeBundle(withField), {
    tsIso: M1,
    rows: [],
    gexField: {
      grid_start: 7595,
      grid_step: 5,
      col_start: M1,
      col_step_min: 10,
      col_count: 1,
      values: [9, 8],
    },
  })
  expect(appended.gexField).toMatchObject({ gridStart: 7595, colCount: 1, values: [9, 8] })

  // Nekonzistentní pole (délka nedělitelná počtem sloupců) se zahodí
  const broken = {
    ...bundleFor(CELLS, BARS, LEVELS, FLOW),
    gexfield: [{ ts_min: M0, grid_start: 7600, grid_step: 5, col_start: M1, col_step_min: 10, col_count: 3, values: [1, 2, 3, 4] }], // prettier-ignore
  }
  expect(decodeBundle(broken).gexField).toBeNull()
  // Starší API bez klíče gexfield → null, nic nepadá
  expect(decodeBundle(bundleFor(CELLS, BARS, LEVELS, FLOW)).gexField).toBeNull()
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
  // Matice mají kapacitní stride (#515) — index = strikeIdx * stride + minuteIdx
  const stride = day.raw.stride ?? day.raw.minutes
  // Stará buňka 7610 C v minutě 0 zůstala (strikeIdx 1, minuteIdx 0)
  expect(day.raw.callOi[1 * stride + 0]).toBe(80)
  // Nový strike 7620 C v minutě 1 (strikeIdx 2, minuteIdx 1)
  expect(day.raw.callOi[2 * stride + 1]).toBe(40)
})

test('appendMinute (#515): živý append i přepis jedou in-place, bez realokace matic', () => {
  const inputs = decodeBundle(
    bundleFor(
      CELLS.filter((c) => c.ts === M0),
      [BARS[0]],
      [LEVELS[0]],
      [FLOW[0]],
    ),
  )
  const matrixBefore = inputs.callOi
  const appended = appendMinute(inputs, {
    tsIso: M1,
    rows: [{ strike: 7600, right: 'C', oi: 100, volume: 30, delta: 0.5 }],
  })
  expect(appended.callOi).toBe(matrixBefore) // tatáž matice, žádná kopie dne
  expect(appended.minuteCapacity).toBe(inputs.minuteCapacity)
  // Přepis existující minuty (WS flush) — taky in-place
  const overwritten = appendMinute(appended, {
    tsIso: M1,
    rows: [{ strike: 7600, right: 'C', oi: 100, volume: 35, delta: 0.5 }],
  })
  expect(overwritten.callOi).toBe(matrixBefore)
  // Nový strike osu mění → pomalá cesta s novými maticemi
  const withNewStrike = appendMinute(overwritten, {
    tsIso: '2026-07-16T15:02:00Z',
    rows: [{ strike: 7620, right: 'C', oi: 40, volume: 15, delta: 0.6 }],
  })
  expect(withNewStrike.callOi).not.toBe(matrixBefore)
})

test('appendMinute (#515): překročení kapacity → růst matic a správné hodnoty', () => {
  let inputs = decodeBundle(
    bundleFor(
      CELLS.filter((c) => c.ts === M0),
      [BARS[0]],
      [LEVELS[0]],
      [FLOW[0]],
    ),
  )
  const capacity = inputs.minuteCapacity
  const isoAt = (i: number): string =>
    new Date(Date.parse(M0) + i * 60_000).toISOString().replace('.000Z', 'Z')
  // Dojede přesně za hranici kapacity (osa startuje s 1 minutou)
  for (let i = 1; i <= capacity; i += 1) {
    inputs = appendMinute(inputs, {
      tsIso: isoAt(i),
      rows: [{ strike: 7600, right: 'C', oi: 100 + i, volume: 10 + i, delta: 0.5 }],
    })
  }
  expect(inputs.minutes).toHaveLength(capacity + 1)
  expect(inputs.minuteCapacity).toBeGreaterThan(capacity)
  const day = assembleReplayDay(inputs)
  const stride = day.raw.stride ?? day.raw.minutes
  // Poslední minuta nese poslední zapsané OI, první minuta původní data
  expect(day.raw.callOi[0 * stride + capacity]).toBe(100 + capacity)
  expect(day.raw.callOi[0 * stride + 0]).toBe(100)
  // Odvozeniny jedou přes celou (prodlouženou) osu
  expect(day.panels.evoOiCall!).toHaveLength(capacity + 1)
  expect(day.panels.evoOiCall!.at(-1)).toBe(100 + capacity)
  expect(day.panels.optVolCall.at(-1)).toBe(1) // volume roste o 1 na minutu
})

test('assembleReplayDay (#515): přepis starší minuty invaliduje cache odvozenin', () => {
  const first = decodeBundle(bundleFor(CELLS, BARS, LEVELS, FLOW))
  const day1 = assembleReplayDay(first) // naplní cache odvozenin
  expect(day1.panels.optVolCall).toEqual([0, 32]) // (30−10) + (20−8)
  // Přepis minuty 0: nižší výchozí volume 7600 C → přírůstek minuty 1 vzroste
  const overwritten = appendMinute(first, {
    tsIso: M0,
    rows: [{ strike: 7600, right: 'C', oi: 100, volume: 2, delta: 0.5 }],
  })
  const day2 = assembleReplayDay(overwritten)
  expect(day2.panels.optVolCall).toEqual([0, 40]) // (30−2) + (20−8)
  // Plný přepočet bez cache dá totéž — cache nesmí změnit výsledek
  const fresh = assembleReplayDay({ ...overwritten, derived: undefined })
  expect(day2.panels).toEqual(fresh.panels)
  expect(day2.overlays.levels?.find((l) => l.name === 'max_pain')?.series).toEqual(
    fresh.overlays.levels?.find((l) => l.name === 'max_pain')?.series,
  )
  // Snímek dne z doby před přepisem drží původní řady (kopie, ne sdílené pole)
  expect(day1.panels.optVolCall).toEqual([0, 32])
})

test('oiTotalSeries: Σ přes striky per minuta, minuta bez snapshotu drží schod (#573)', () => {
  // 2 striky × 3 minuty; minuta 1 bez snapshotu — nesmí spadnout na nulu
  const oi = Float32Array.from([100, 0, 120, 50, 0, 40]) // strike0: [100,0,120], strike1: [50,0,40]
  const series = oiTotalSeries(oi, 3, 2, (minuteIdx) => minuteIdx !== 1)
  expect(series).toEqual([150, 150, 160])
})

test('accumulatePrintVol (#1007): kumulativ per buňka, díra dědí, NULL otráví NaN', () => {
  const capacity = 4
  const target = {
    callPrinted: new Float32Array(1 * capacity),
    putPrinted: new Float32Array(1 * capacity),
    callStructured: new Float32Array(1 * capacity),
    putStructured: new Float32Array(1 * capacity),
  }
  accumulatePrintVol(
    [
      { minuteIdx: 0, strikeIdx: 0, right: 'C', printed: 5, structured: 1 },
      { minuteIdx: 2, strikeIdx: 0, right: 'C', printed: 3, structured: 2 },
      { minuteIdx: 1, strikeIdx: 0, right: 'P', printed: null, structured: null },
      { minuteIdx: 2, strikeIdx: 0, right: 'P', printed: 7, structured: 0 },
      // neznámý strike/minuta se přeskočí
      { minuteIdx: undefined, strikeIdx: 0, right: 'C', printed: 99, structured: 99 },
    ],
    target,
    1,
    capacity,
    3,
  )
  expect(Array.from(target.callPrinted.subarray(0, 3))).toEqual([5, 5, 8])
  expect(Array.from(target.callStructured.subarray(0, 3))).toEqual([1, 1, 3])
  expect(target.putPrinted[0]).toBe(0)
  expect(Number.isNaN(target.putPrinted[1])).toBe(true)
  expect(Number.isNaN(target.putPrinted[2])).toBe(true) // NaN se dědí i přes další řádek
})

test('outrightShareAt (#1007): podíl tisků, NaN a nulový objem → null', () => {
  const printed = new Float32Array([60, Number.NaN, 0])
  const structured = new Float32Array([40, 1, 0])
  expect(outrightShareAt(printed, structured, 0)).toBeCloseTo(0.6)
  expect(outrightShareAt(printed, structured, 1)).toBeNull()
  expect(outrightShareAt(printed, structured, 2)).toBeNull()
  expect(outrightShareAt(undefined, structured, 0)).toBeNull()
})
