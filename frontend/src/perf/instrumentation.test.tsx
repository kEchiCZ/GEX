/** Počítadla měřiče výkonu nad skutečným kódem grafu (#1409).

Počty jsou deterministické — výkonový rozpočet v CI (E-2a.13) na ně spoléhá;
časy se neověřují. */
import { render } from '@testing-library/react'
import { tableFromArrays, tableToIPC } from 'apache-arrow'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { Heatmap } from '../components/Heatmap'
import { demoGrid } from '../heatmap/demo'
import { CrosshairProvider } from '../state/Crosshair'
import { assembleReplayDay, fetchReplayInputs } from '../replay/loader'
import { perfBegin, perfCounters, resetPerf, setPerfEnabled } from './meter'

beforeEach(() => {
  resetPerf()
  setPerfEnabled(true)
})

afterEach(() => {
  setPerfEnabled(false)
  resetPerf()
  vi.unstubAllGlobals()
})

function bundle() {
  const table = tableFromArrays({
    ts_min: ['2026-07-16T15:00:00Z', '2026-07-16T15:00:00Z', '2026-07-16T15:01:00Z', '2026-07-16T15:01:00Z'], // prettier-ignore
    strike: Float64Array.from([7600, 7600, 7600, 7600]),
    right: ['C', 'P', 'C', 'P'],
    volume: Float64Array.from([10, 5, 30, 12]),
    oi: Float64Array.from([100, 200, 100, 200]),
    delta: Float64Array.from([0.5, -0.4, 0.5, -0.4]),
  })
  return {
    symbol: 'ES',
    expiry: '20260716',
    date: '2026-07-16',
    snapshots_arrow_base64: btoa(String.fromCharCode(...tableToIPC(table, 'stream'))),
    levels: [],
    flow: [],
    bars: [
      { ts_min: '2026-07-16T15:00:00Z', close: 7600.5, volume: 1000 },
      { ts_min: '2026-07-16T15:01:00Z', close: 7601.5, volume: 1200 },
    ],
    oi_prev: [],
  }
}

async function loadDay() {
  const inputs = await fetchReplayInputs('ES', '20260716', '2026-07-16')
  return assembleReplayDay(inputs)
}

test('fáze načtení dne: fetch, dekódování a sestavení se počítají deterministicky', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve(bundle()) }),
  )

  await loadDay()
  const once = perfCounters()
  expect(once).toMatchObject({ dayFetch: 1, dayDecode: 1, dayBuild: 1 })
  expect(once.buildModeGrid).toBeGreaterThan(0)

  await loadDay()
  const twice = perfCounters()
  for (const [name, count] of Object.entries(once)) expect(twice[name]).toBe(count * 2)
})

test('Heatmap: rastr a obě vrstvy overlay se počítají deterministicky', () => {
  const grid = demoGrid(100, 10)
  const { rerender } = render(
    <CrosshairProvider>
      <Heatmap grid={grid} style="gradient" contours="off" />
    </CrosshairProvider>,
  )
  expect(perfCounters()).toEqual({ renderHeatmap: 1, renderGrid: 1, drawStatic: 1, drawDynamic: 1 })

  // Týž grid: rastr se nepřepočítá. Overlay se dnes překreslí znovu (výchozí
  // props bez memo dávají nové identity) — přesně tohle má měřič ukazovat
  // pro opravy E-2a.3 a E-2a.4
  rerender(
    <CrosshairProvider>
      <Heatmap grid={grid} style="gradient" contours="off" />
    </CrosshairProvider>,
  )
  expect(perfCounters()).toEqual({ renderHeatmap: 2, renderGrid: 1, drawStatic: 2, drawDynamic: 2 })
})

test('Mode/Scale span končí vykreslením mřížky s hotovými konturami', () => {
  const { rerender } = render(
    <CrosshairProvider>
      <Heatmap grid={demoGrid(100, 10)} style="gradient" contours="major" />
    </CrosshairProvider>,
  )
  perfBegin('modeScale')
  // Bez Workeru (jsdom) se kontury spočtou synchronně → span končí hned
  rerender(
    <CrosshairProvider>
      <Heatmap grid={demoGrid(100, 10)} style="gradient" contours="major" />
    </CrosshairProvider>,
  )
  expect(perfCounters().modeScale).toBe(1)
  expect(perfCounters().renderGrid).toBe(2)
})
