/** Daily pohled (#1206): denní součty z API mají přednost před sčítáním minutových panelů. */
import { expect, test } from 'vitest'
import { buildDailyDay } from './daily'
import { assembleReplayDay, decodeBundle } from './loader'
import type { ReplayBundle, ReplayDay } from './loader'
import { tableToIPC, tableFromArrays } from 'apache-arrow'

function bundle(date: string, daily?: ReplayBundle['daily']): ReplayBundle {
  const table = tableFromArrays({
    ts_min: [`${date}T15:00:00Z`, `${date}T15:00:00Z`],
    strike: [7600, 7600],
    right: ['C', 'P'],
    volume: [10, 20],
    oi: [100, 150],
    delta: [0.5, -0.4],
    stale_age: [0, 0],
    vega: [1, 1],
    bid: [1, 1],
    ask: [1.5, 1.5],
    gamma: [0.01, 0.01],
    iv: [0.2, 0.2],
    last: [1.2, 1.2],
    theta: [-0.1, -0.1],
  })
  const bytes = tableToIPC(table, 'stream')
  let binary = ''
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte)
  })
  return {
    symbol: 'ES',
    expiry: date.replaceAll('-', ''),
    date,
    snapshots_arrow_base64: btoa(binary),
    levels: [],
    flow: [],
    bars: [
      { ts_min: `${date}T15:00:00Z`, open: 7600, high: 7605, low: 7599, close: 7602, volume: 40 },
    ],
    daily,
  } as ReplayBundle
}

function day(date: string, daily?: ReplayBundle['daily']): ReplayDay {
  return assembleReplayDay(decodeBundle(bundle(date, daily)))
}

test('bez součtů se panely sčítají z minut (plný balík)', () => {
  const built = buildDailyDay([day('2026-07-16')])
  expect(built.panels.vol).toEqual([40])
  expect(built.panels.evoOiCall).toEqual([100])
  expect(built.overlays.price?.[0]).toMatchObject({
    open: 7600,
    close: 7602,
    high: 7605,
    low: 7599,
  })
})

test('součty z ?resolution=daily mají přednost a nesou denní OHLC (#1206)', () => {
  const totals: ReplayBundle['daily'] = {
    ts_min: '2026-07-16T15:00:00Z',
    vol: 91927,
    opt_vol_call: 1200,
    opt_vol_put: 900,
    delta_flow_call: 600,
    delta_flow_put: 360,
    evo_oi_call: 5000,
    evo_oi_put: 7000,
    cum_delta: -107167,
    bar: { open: 7624, high: 7670.25, low: 7617.5, close: 7662.25 },
  }
  const built = buildDailyDay([day('2026-07-15'), day('2026-07-16', totals)])
  expect(built.panels.vol).toEqual([40, 91927])
  expect(built.panels.optVolCall[1]).toBe(1200)
  expect(built.panels.optVolPut[1]).toBe(900)
  expect(built.panels.deltaFlowCall[1]).toBe(600)
  expect(built.panels.deltaFlowPut[1]).toBe(360)
  expect(built.panels.cumDelta[1]).toBe(-107167)
  expect(built.panels.evoOiCall?.[1]).toBe(5000)
  expect(built.panels.evoOiPut?.[1]).toBe(7000)
  const bar = built.overlays.price?.[1]
  expect(bar).toMatchObject({
    minuteIdx: 1,
    open: 7624,
    high: 7670.25,
    low: 7617.5,
    close: 7662.25,
  })
  // up = close nad předchozím close (7602)
  expect(bar?.up).toBe(true)
  expect(built.spotSeries[1]).toBe(7662.25)
})
