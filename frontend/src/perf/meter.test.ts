import { afterEach, beforeEach, expect, test } from 'vitest'
import {
  PERF_WINDOW_MS,
  perfBegin,
  perfCounters,
  perfEnd,
  perfExport,
  perfFinish,
  perfRecordFrame,
  perfRecordLongtask,
  perfRecords,
  perfStart,
  perfSummary,
  resetPerf,
  setPerfClock,
  setPerfEnabled,
  setPerfView,
} from './meter'

let now = 0
const advance = (ms: number) => {
  now += ms
}

beforeEach(() => {
  now = 1_000
  setPerfClock(() => now)
  setPerfEnabled(false)
  resetPerf()
  setPerfView({ symbol: 'ES', timeframe: '1m', mode: 'oi' })
})

afterEach(() => {
  setPerfEnabled(false)
  resetPerf()
  setPerfClock(() => performance.now())
})

test('vypnutý měřič nic nezapisuje ani nepočítá', () => {
  const t0 = perfStart()
  advance(5)
  perfEnd('drawStatic', t0)
  perfBegin('dayLoad')
  perfFinish('dayLoad')
  perfRecordFrame(now)
  perfRecordLongtask(now, 80)

  expect(perfRecords(now)).toEqual([])
  expect(perfCounters()).toEqual({})
})

test('operace mají deterministická počítadla a záznam s pohledem', () => {
  setPerfEnabled(true)
  for (let i = 0; i < 3; i += 1) {
    const t0 = perfStart()
    advance(4)
    perfEnd('drawStatic', t0)
  }
  const t0 = perfStart()
  advance(10)
  perfEnd('buildModeGrid', t0)

  expect(perfCounters()).toEqual({ drawStatic: 3, buildModeGrid: 1 })
  const ops = perfSummary(now).ops
  expect(ops.drawStatic).toEqual({ count: 3, p50: 4, p95: 4, max: 4 })
  expect(ops.buildModeGrid.max).toBe(10)
  expect(perfRecords(now).at(-1)?.view).toEqual({ symbol: 'ES', timeframe: '1m', mode: 'oi' })
})

test('operace začatá před zapnutím se nezapíše', () => {
  const t0 = perfStart()
  setPerfEnabled(true)
  perfEnd('renderGrid', t0)
  expect(perfCounters()).toEqual({})
})

test('buffer drží posledních 30 s a starší záznamy zahazuje', () => {
  setPerfEnabled(true)
  // 45 s snímků po 16 ms — přes práh kompakce pole
  for (let i = 0; i < 2_800; i += 1) {
    advance(16)
    perfRecordFrame(now)
  }
  const kept = perfRecords(now)
  expect(kept.length).toBeGreaterThan(0)
  expect(kept[0].t).toBeGreaterThanOrEqual(now - PERF_WINDOW_MS)
  expect(kept.length).toBe(Math.floor(PERF_WINDOW_MS / 16) + 1)
  // Snímky za pokrytou dobu: 1000 ms / 16 ms = 62,5 fps
  expect(perfSummary(now).frames.fps).toBeCloseTo(62.5, 0)
  expect(perfSummary(now).frames.p95).toBe(16)
})

test('p50 a p95 délky snímku', () => {
  setPerfEnabled(true)
  perfRecordFrame(now)
  for (const ms of [10, 10, 10, 10, 10, 10, 10, 10, 10, 50]) {
    advance(ms)
    perfRecordFrame(now)
  }
  const frames = perfSummary(now).frames
  expect(frames.count).toBe(10)
  expect(frames.p50).toBe(10)
  expect(frames.p95).toBe(50)
  expect(frames.max).toBe(50)
})

test('span do vykreslení: začátek, konec, přepsání a konec bez začátku', () => {
  setPerfEnabled(true)
  perfFinish('symbolSwitch') // bez začátku — nic
  perfBegin('modeScale')
  advance(30)
  perfBegin('modeScale') // nové přepnutí přepíše rozpracované
  advance(40)
  perfFinish('modeScale')
  perfFinish('modeScale') // druhý konec — nic

  expect(perfCounters()).toEqual({ modeScale: 1 })
  expect(perfSummary(now).spans.modeScale).toEqual({ count: 1, p50: 40, p95: 40, max: 40 })
})

test('vypnutí zahodí rozpracované spany, data drží do resetu', () => {
  setPerfEnabled(true)
  perfBegin('dayLoad')
  const t0 = perfStart()
  advance(2)
  perfEnd('dayFetch', t0)
  setPerfEnabled(false)
  setPerfEnabled(true)
  perfFinish('dayLoad')

  expect(perfCounters()).toEqual({ dayFetch: 1 })
  resetPerf()
  expect(perfCounters()).toEqual({})
  expect(perfRecords(now)).toEqual([])
})

test('dlouhé úlohy: počet a maximum v bufferu i za minutu', () => {
  setPerfEnabled(true)
  perfRecordLongtask(now, 60)
  advance(1_000)
  perfRecordLongtask(now, 120)

  const longtasks = perfSummary(now).longtasks
  expect(longtasks.count).toBe(2)
  expect(longtasks.maxMs).toBe(120)
  const total = longtasks.perMinute.reduce((sum, minute) => sum + minute.count, 0)
  expect(total).toBe(2)
  expect(Math.max(...longtasks.perMinute.map((minute) => minute.maxMs))).toBe(120)
})

test('export JSON nese čas, pohled, verzi buildu a počítadla', () => {
  setPerfEnabled(true)
  const t0 = perfStart()
  advance(3)
  perfEnd('drawDynamic', t0)
  setPerfView({ symbol: 'NQ', timeframe: '5m', mode: 'gex' })
  perfBegin('symbolSwitch')
  advance(7)
  perfFinish('symbolSwitch')

  const exported = JSON.parse(JSON.stringify(perfExport(now))) as ReturnType<typeof perfExport>
  expect(exported.schema).toBe(1)
  expect(exported.build).toBe('dev')
  expect(exported.view).toEqual({ symbol: 'NQ', timeframe: '5m', mode: 'gex' })
  expect(exported.counters).toEqual({ drawDynamic: 1, symbolSwitch: 1 })
  expect(Number.isNaN(Date.parse(exported.exportedAt))).toBe(false)
  expect(exported.records.map((record) => [record.name, record.view.symbol])).toEqual([
    ['drawDynamic', 'ES'],
    ['symbolSwitch', 'NQ'],
  ])
  expect(Number.isNaN(Date.parse(exported.records[0].at))).toBe(false)
})
