/** Bench minutového updatu (#515): appendMinute + assembleReplayDay na syntetickém dni.
 *
 * Spuštění: npx vite-node scripts/bench-append.ts  (z adresáře frontend/)
 * Stejný skript běží na main i na větvi — čísla před/po patří do PR.
 */
import { tableFromArrays, tableToIPC } from 'apache-arrow'
import { appendMinute, assembleReplayDay, decodeBundle } from '../src/replay/loader'
import type { LiveMinute } from '../src/replay/loader'

const STRIKES = 250
const MINUTES = 900
const APPENDS = 30

function isoAt(i: number): string {
  return new Date(Date.UTC(2026, 6, 16, 0, 0) + i * 60_000).toISOString().replace('.000Z', 'Z')
}

function syntheticBundle(): Parameters<typeof decodeBundle>[0] {
  const rows = STRIKES * MINUTES * 2
  const ts: string[] = new Array(rows)
  const strike = new Float64Array(rows)
  const right: string[] = new Array(rows)
  const volume = new Float64Array(rows)
  const oi = new Float64Array(rows)
  const delta = new Float64Array(rows)
  const stale = new Float64Array(rows)
  let row = 0
  for (let m = 0; m < MINUTES; m += 1) {
    for (let s = 0; s < STRIKES; s += 1) {
      for (const side of ['C', 'P']) {
        ts[row] = isoAt(m)
        strike[row] = 7000 + s * 5
        right[row] = side
        volume[row] = m + s
        oi[row] = 100 + ((s * 7) % 50)
        delta[row] = side === 'C' ? 0.5 : -0.4
        stale[row] = 0
        row += 1
      }
    }
  }
  const table = tableFromArrays({
    ts_min: ts,
    strike,
    right,
    volume,
    oi,
    delta,
    stale_age: stale,
  })
  return {
    symbol: 'ES',
    expiry: '20260716',
    date: '2026-07-16',
    snapshots_arrow_base64: Buffer.from(tableToIPC(table, 'stream')).toString('base64'),
    levels: Array.from({ length: MINUTES }, (_, m) => ({
      ts_min: isoAt(m),
      flip: 7595,
      centroid: 7598,
      call_wall: 7650,
      put_wall: 7500,
    })),
    flow: Array.from({ length: MINUTES }, (_, m) => ({ ts_min: isoAt(m), cum_delta: m })),
    bars: Array.from({ length: MINUTES }, (_, m) => ({
      ts_min: isoAt(m),
      close: 7600 + (m % 10),
      volume: 1000,
    })),
  }
}

function liveMinute(i: number): LiveMinute {
  return {
    tsIso: isoAt(MINUTES + i),
    rows: Array.from({ length: STRIKES * 2 }, (_, j) => ({
      strike: 7000 + Math.floor(j / 2) * 5,
      right: j % 2 === 0 ? ('C' as const) : ('P' as const),
      oi: 100,
      volume: MINUTES + i + Math.floor(j / 2),
      delta: j % 2 === 0 ? 0.5 : -0.4,
    })),
    bar: { close: 7600, volume: 1000 },
    flow: { cum_delta: MINUTES + i },
  }
}

const t0 = performance.now()
let inputs = decodeBundle(syntheticBundle())
assembleReplayDay(inputs)
const tDecode = performance.now() - t0

const appendTimes: number[] = []
const assembleTimes: number[] = []
for (let i = 0; i < APPENDS; i += 1) {
  const minute = liveMinute(i)
  const a0 = performance.now()
  inputs = appendMinute(inputs, minute)
  const a1 = performance.now()
  assembleReplayDay(inputs)
  const a2 = performance.now()
  appendTimes.push(a1 - a0)
  assembleTimes.push(a2 - a1)
}

const stats = (values: number[]): string => {
  const sorted = [...values].sort((a, b) => a - b)
  const avg = values.reduce((s, v) => s + v, 0) / values.length
  return `avg ${avg.toFixed(2)} ms · p50 ${sorted[Math.floor(values.length / 2)].toFixed(2)} ms · max ${sorted.at(-1)!.toFixed(2)} ms`
}

console.log(`den ${STRIKES} striků × ${MINUTES} minut, ${APPENDS} živých appendů`)
console.log(`decode+assemble startu: ${tDecode.toFixed(0)} ms`)
console.log(`appendMinute:      ${stats(appendTimes)}`)
console.log(`assembleReplayDay: ${stats(assembleTimes)}`)
console.log(
  `minutový update celkem (append+assemble): ${stats(appendTimes.map((v, i) => v + assembleTimes[i]))}`,
)
