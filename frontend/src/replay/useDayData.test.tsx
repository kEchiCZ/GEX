/** Testy živého intraday: WS append minut (#127) + hodinová pojistka refetch. */
import { StrictMode } from 'react'
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import type { LiveSocket } from '../api/ws'
import { useDayData } from './useDayData'
import { fetchDays, fetchReplay, fetchReplayInputs } from './loader'
import type { ReplayInputs } from './loader'

vi.mock('./loader', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./loader')>()
  return { ...actual, fetchReplayInputs: vi.fn(), fetchReplay: vi.fn(), fetchDays: vi.fn() }
})

/** Minimální vstup: 1 strike × 1 minuta. */
function makeInputs(): ReplayInputs {
  return {
    symbol: 'ES',
    expiry: '20260716',
    date: '2026-07-16',
    minutes: ['2026-07-16T15:00:00.000Z'],
    strikes: [7600],
    callOi: new Float32Array([100]),
    putOi: new Float32Array([200]),
    callVolume: new Float32Array([10]),
    putVolume: new Float32Array([5]),
    callDelta: new Float32Array([0.5]),
    putDelta: new Float32Array([-0.4]),
    staleAge: new Float32Array([0]),
    bars: [{ tsIso: '2026-07-16T15:00:00.000Z', close: 7600.5, volume: 1000 }],
    levels: [],
    flow: [],
    oiPrev: [],
  }
}

type FakeSocket = LiveSocket & { emit: (channel: string, data: unknown) => void }

function makeSocket(): FakeSocket {
  const handlers = new Map<string, Set<(data: unknown) => void>>()
  const socket = {
    subscribe: (channel: string, handler: (data: unknown) => void) => {
      let set = handlers.get(channel)
      if (!set) {
        set = new Set()
        handlers.set(channel, set)
      }
      set.add(handler)
    },
    unsubscribe: (channel: string, handler: (data: unknown) => void) =>
      handlers.get(channel)?.delete(handler),
    onReconnect: () => () => {},
    connect: () => {},
    close: () => {},
    emit: (channel: string, data: unknown) => handlers.get(channel)?.forEach((h) => h(data)),
  }
  return socket as unknown as FakeSocket
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

test('WS snapshot+price přidá novou minutu (append, ne refetch) — issue #127', async () => {
  vi.mocked(fetchReplayInputs).mockResolvedValue(makeInputs())
  const socket = makeSocket()
  const { result } = renderHook(() =>
    useDayData('ES', '20260716', '2026-07-16', 'intraday', socket),
  )
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0) // úvodní fetch
  })
  expect(result.current.day.grid.minutes).toBe(1)
  expect(fetchReplayInputs).toHaveBeenCalledTimes(1)

  await act(async () => {
    socket.emit('snapshot.ES.20260716', {
      ts_min: '2026-07-16T15:01:00Z',
      rows: [
        { strike: 7600, right: 'C', oi: 100, volume: 30, delta: 0.5 },
        { strike: 7600, right: 'P', oi: 200, volume: 12, delta: -0.4 },
      ],
    })
    socket.emit('price.ES', {
      ts: '2026-07-16T15:01:00Z',
      open: 7600.5,
      high: 7603,
      low: 7600,
      close: 7602,
      volume: 1300,
    })
    await vi.advanceTimersByTimeAsync(500) // debounce flush
  })
  expect(result.current.day.grid.minutes).toBe(2) // minuta přibyla appendem
  expect(fetchReplayInputs).toHaveBeenCalledTimes(1) // bez dalšího refetche
})

test('plný refetch je hodinová pojistka, ne každou minutu — issue #127', async () => {
  vi.mocked(fetchReplayInputs).mockResolvedValue(makeInputs())
  renderHook(() => useDayData('ES', '20260716', '2026-07-16', 'intraday', makeSocket()))
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(fetchReplayInputs).toHaveBeenCalledTimes(1)

  await act(async () => {
    await vi.advanceTimersByTimeAsync(120_000) // 2 minuty → žádný refetch
  })
  expect(fetchReplayInputs).toHaveBeenCalledTimes(1)

  await act(async () => {
    await vi.advanceTimersByTimeAsync(3_600_000) // hodina → pojistný refetch
  })
  expect(fetchReplayInputs).toHaveBeenCalledTimes(2)
})

test('živý spot přidá rozdělanou svíčku na náběžnou hranu a uzavře ji snapshotem (#128)', async () => {
  vi.mocked(fetchReplayInputs).mockResolvedValue(makeInputs())
  const socket = makeSocket()
  const { result } = renderHook(() =>
    useDayData('ES', '20260716', '2026-07-16', 'intraday', socket),
  )
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  const staticPrice = result.current.day.overlays.price!
  expect(result.current.day.grid.minutes).toBe(1)
  expect(result.current.live.bars).toHaveLength(0)

  // spot v nové (neuzavřené) minutě 15:01 → rozdělaná svíčka v ŽIVÉ vrstvě (#141)
  await act(async () => {
    socket.emit('spot.ES', { ts: '2026-07-16T15:01:30Z', price: 7602 })
  })
  let live = result.current.live.bars
  expect(live).toHaveLength(1)
  expect(live[0].minuteIdx).toBe(1) // náběžná hrana = grid.minutes
  expect(live[0].open).toBe(7602)
  expect(live[0].close).toBe(7602)
  // Statická data se spot tickem nesmí dotknout — jinak by se překreslila statická vrstva
  expect(result.current.day.overlays.price).toBe(staticPrice)

  // další ticky téže minuty: aktualizují high/low/close, žádná další svíčka
  await act(async () => {
    socket.emit('spot.ES', { ts: '2026-07-16T15:01:45Z', price: 7605 })
    socket.emit('spot.ES', { ts: '2026-07-16T15:01:50Z', price: 7601 })
  })
  live = result.current.live.bars
  expect(live).toHaveLength(1)
  expect(live[0].high).toBe(7605)
  expect(live[0].low).toBe(7601)
  expect(live[0].close).toBe(7601)
  expect(result.current.day.overlays.price).toBe(staticPrice)

  // minuta se uzavře (snapshot+price) → svíčka přechází do statické vrstvy
  await act(async () => {
    socket.emit('snapshot.ES.20260716', {
      ts_min: '2026-07-16T15:01:00Z',
      rows: [{ strike: 7600, right: 'C', oi: 100, volume: 30, delta: 0.5 }],
    })
    socket.emit('price.ES', {
      ts: '2026-07-16T15:01:00Z',
      open: 7602,
      high: 7605,
      low: 7601,
      close: 7601,
      volume: 1300,
    })
    await vi.advanceTimersByTimeAsync(500)
  })
  expect(result.current.day.grid.minutes).toBe(2)
  expect(result.current.live.bars).toHaveLength(0)
  expect(result.current.day.overlays.price!.filter((bar) => bar.minuteIdx === 1)).toHaveLength(1)
})

test('statická data drží identitu napříč spot ticky — dynamická vrstva (#141)', async () => {
  vi.mocked(fetchReplayInputs).mockResolvedValue(makeInputs())
  const socket = makeSocket()
  const { result } = renderHook(() =>
    useDayData('ES', '20260716', '2026-07-16', 'intraday', socket),
  )
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  const day = result.current.day

  await act(async () => {
    socket.emit('spot.ES', { ts: '2026-07-16T15:01:10Z', price: 7602 })
    socket.emit('spot.ES', { ts: '2026-07-16T15:01:20Z', price: 7607 })
    socket.emit('spot.ES', { ts: '2026-07-16T15:01:30Z', price: 7599 })
  })
  // Celý statický den (grid, overlays, panely, popisky os) musí zůstat TOTOŽNÝ objekt —
  // na tom stojí memoizace statické vrstvy overlaye v Heatmapě (#141).
  expect(result.current.day).toBe(day)
  expect(result.current.live.bars).toHaveLength(1)
  expect(result.current.live.bars[0].close).toBe(7599)
  expect(result.current.live.labels).toHaveLength(1)
})

test('bar dorazí v jiném flushi než snapshot — svíčka se přesto objeví (#133)', async () => {
  vi.mocked(fetchReplayInputs).mockResolvedValue(makeInputs())
  const socket = makeSocket()
  const { result } = renderHook(() =>
    useDayData('ES', '20260716', '2026-07-16', 'intraday', socket),
  )
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })

  // Snapshot minuty 15:01 (bez baru) → mřížka roste, svíčka pro tu minutu zatím není
  await act(async () => {
    socket.emit('snapshot.ES.20260716', {
      ts_min: '2026-07-16T15:01:00Z',
      rows: [{ strike: 7600, right: 'C', oi: 100, volume: 30, delta: 0.5 }],
    })
    await vi.advanceTimersByTimeAsync(500)
  })
  expect(result.current.day.grid.minutes).toBe(2)
  expect(result.current.day.overlays.price!.filter((b) => b.minuteIdx === 1)).toHaveLength(0)

  // Bar téže minuty dorazí až v dalším flushi → musí se aplikovat na existující minutu
  await act(async () => {
    socket.emit('price.ES', {
      ts: '2026-07-16T15:01:00Z',
      open: 7601,
      high: 7605,
      low: 7600,
      close: 7602,
      volume: 1300,
    })
    await vi.advanceTimersByTimeAsync(500)
  })
  const candle = result.current.day.overlays.price!.filter((b) => b.minuteIdx === 1)
  expect(candle).toHaveLength(1) // svíčka se objevila, nezmizela
  expect(candle[0].close).toBe(7602)
})

test('StrictMode: uzavřená minuta se neztratí dvojím během updateru (#143)', async () => {
  vi.mocked(fetchReplayInputs).mockResolvedValue(makeInputs())
  const socket = makeSocket()
  const { result } = renderHook(
    () => useDayData('ES', '20260716', '2026-07-16', 'intraday', socket),
    { wrapper: StrictMode },
  )
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(result.current.day.grid.minutes).toBe(1)

  await act(async () => {
    socket.emit('snapshot.ES.20260716', {
      ts_min: '2026-07-16T15:01:00Z',
      rows: [{ strike: 7600, right: 'C', oi: 100, volume: 30, delta: 0.5 }],
    })
    socket.emit('price.ES', {
      ts: '2026-07-16T15:01:00Z',
      open: 7600.5,
      high: 7603,
      low: 7600,
      close: 7602,
      volume: 1300,
    })
    await vi.advanceTimersByTimeAsync(500)
  })
  // Updater se ve StrictMode volá dvakrát — minuta i její svíčka musí přežít oba průběhy
  expect(result.current.day.grid.minutes).toBe(2)
  expect(result.current.day.overlays.price!.filter((bar) => bar.minuteIdx === 1)).toHaveLength(1)
})

test('uzavřená minuta bez baru drží svíčku ze spotu, dokud bar nedorazí (#143)', async () => {
  vi.mocked(fetchReplayInputs).mockResolvedValue(makeInputs())
  const socket = makeSocket()
  const { result } = renderHook(() =>
    useDayData('ES', '20260716', '2026-07-16', 'intraday', socket),
  )
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })

  // Spot v rozdělané minutě 15:01
  await act(async () => {
    socket.emit('spot.ES', { ts: '2026-07-16T15:01:20Z', price: 7602 })
    socket.emit('spot.ES', { ts: '2026-07-16T15:01:50Z', price: 7605 })
  })
  expect(result.current.live.bars.at(-1)!.minuteIdx).toBe(1)

  // Minuta se uzavře snapshotem, ale bar z price kanálu (zatím) nedorazil —
  // svíčka nesmí zmizet, jinak v grafu chybí předchozí svíce (#143)
  await act(async () => {
    socket.emit('snapshot.ES.20260716', {
      ts_min: '2026-07-16T15:01:00Z',
      rows: [{ strike: 7600, right: 'C', oi: 100, volume: 30, delta: 0.5 }],
    })
    await vi.advanceTimersByTimeAsync(500)
  })
  expect(result.current.day.grid.minutes).toBe(2)
  expect(result.current.day.overlays.price!.filter((bar) => bar.minuteIdx === 1)).toHaveLength(0)
  const fallback = result.current.live.bars.filter((bar) => bar.minuteIdx === 1)
  expect(fallback).toHaveLength(1)
  expect(fallback[0].high).toBe(7605)

  // Skutečný bar dorazí později → nahradí spot zálohu (jediná svíčka, hodnoty z baru)
  await act(async () => {
    socket.emit('price.ES', {
      ts: '2026-07-16T15:01:00Z',
      open: 7601,
      high: 7606,
      low: 7600,
      close: 7603,
      volume: 1300,
    })
    await vi.advanceTimersByTimeAsync(500)
  })
  const real = result.current.day.overlays.price!.filter((bar) => bar.minuteIdx === 1)
  expect(real).toHaveLength(1)
  expect(real[0].close).toBe(7603)
  expect(real[0].high).toBe(7606)
  expect(result.current.live.bars).toHaveLength(0) // záloha zanikla
})

test('provizorní bar zaplní minutu, ale nezastaví živou svíčku (ADR-0005)', async () => {
  vi.mocked(fetchReplayInputs).mockResolvedValue(makeInputs())
  const socket = makeSocket()
  const { result } = renderHook(() =>
    useDayData('ES', '20260716', '2026-07-16', 'intraday', socket),
  )
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })

  // Spot běží v rozdělané minutě 15:01
  await act(async () => {
    socket.emit('spot.ES', { ts: '2026-07-16T15:01:20Z', price: 7602 })
  })
  expect(result.current.live.bars.at(-1)!.minuteIdx).toBe(1)

  // Engine pošle snapshot 15:01 + PROVIZORNÍ bar téže minuty (final: false)
  await act(async () => {
    socket.emit('snapshot.ES.20260716', {
      ts_min: '2026-07-16T15:01:00Z',
      rows: [{ strike: 7600, right: 'C', oi: 100, volume: 30, delta: 0.5 }],
    })
    socket.emit('price.ES', {
      ts: '2026-07-16T15:01:00Z',
      open: 7601,
      high: 7604,
      low: 7600,
      close: 7603,
      volume: 900,
      final: false,
    })
    await vi.advanceTimersByTimeAsync(500)
  })
  // Minuta má bar (přežije refresh), ale spot svíčka ji dál přebíjí — je živější
  expect(result.current.day.overlays.price!.some((bar) => bar.minuteIdx === 1)).toBe(true)
  expect(result.current.live.bars.some((bar) => bar.minuteIdx === 1)).toBe(true)

  // Další spot tick se pořád propisuje
  await act(async () => {
    socket.emit('spot.ES', { ts: '2026-07-16T15:01:50Z', price: 7609 })
  })
  expect(result.current.live.bars.find((bar) => bar.minuteIdx === 1)!.high).toBe(7609)

  // Finální bar minutu uzavře → spot záloha zaniká
  await act(async () => {
    socket.emit('price.ES', {
      ts: '2026-07-16T15:01:00Z',
      open: 7601,
      high: 7610,
      low: 7600,
      close: 7607,
      volume: 1300,
      final: true,
    })
    await vi.advanceTimersByTimeAsync(500)
  })
  expect(result.current.live.bars.some((bar) => bar.minuteIdx === 1)).toBe(false)
  const closed = result.current.day.overlays.price!.filter((bar) => bar.minuteIdx === 1)
  expect(closed).toHaveLength(1)
  expect(closed[0].close).toBe(7607)
})

test('bar bez pole final se bere jako finální (starší engine)', async () => {
  vi.mocked(fetchReplayInputs).mockResolvedValue(makeInputs())
  const socket = makeSocket()
  const { result } = renderHook(() =>
    useDayData('ES', '20260716', '2026-07-16', 'intraday', socket),
  )
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  await act(async () => {
    socket.emit('spot.ES', { ts: '2026-07-16T15:01:20Z', price: 7602 })
    socket.emit('snapshot.ES.20260716', {
      ts_min: '2026-07-16T15:01:00Z',
      rows: [{ strike: 7600, right: 'C', oi: 100, volume: 30, delta: 0.5 }],
    })
    socket.emit('price.ES', {
      ts: '2026-07-16T15:01:00Z',
      open: 7601,
      high: 7605,
      low: 7600,
      close: 7602,
      volume: 1300,
    })
    await vi.advanceTimersByTimeAsync(500)
  })
  expect(result.current.live.bars.some((bar) => bar.minuteIdx === 1)).toBe(false)
  expect(result.current.day.overlays.price!.filter((b) => b.minuteIdx === 1)).toHaveLength(1)
})

test('daily režim nepoužívá intraday live fetch', async () => {
  vi.mocked(fetchDays).mockResolvedValue([])
  renderHook(() => useDayData('ES', '20260716', '2026-07-16', 'daily', makeSocket()))
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3_600_000)
  })
  expect(fetchReplayInputs).not.toHaveBeenCalled()
  expect(fetchReplay).not.toHaveBeenCalled()
})
