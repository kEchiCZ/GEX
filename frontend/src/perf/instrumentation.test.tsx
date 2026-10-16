/** Počítadla měřiče výkonu nad skutečným kódem grafu (#1409).

Počty jsou deterministické — výkonový rozpočet v CI (E-2a.13) na ně spoléhá;
časy se neověřují. */
import { render } from '@testing-library/react'
import { tableFromArrays, tableToIPC } from 'apache-arrow'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { BottomPanels } from '../components/BottomPanels'
import { Heatmap } from '../components/Heatmap'
import { StrikeProfile } from '../components/StrikeProfile'
import { demoGrid } from '../heatmap/demo'
import type { HeatmapGrid } from '../heatmap/grid'
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

test('rastr per vrstva: Mode s Dyn podkladem přepočítá jen měřenou vrstvu (#1420)', () => {
  const grid = demoGrid(100, 10)
  const signedOf = (seed: number) =>
    Float32Array.from({ length: grid.minutes * grid.strikes.length }, (_, i) => Math.sin(i / seed))
  const under: HeatmapGrid = { ...grid, layers: { signed: signedOf(7) }, staleAge: null }
  const view = (measured: HeatmapGrid, underlay: HeatmapGrid, style: 'gradient' | 'blobs') => (
    <CrosshairProvider>
      <Heatmap grid={measured} underGrid={underlay} style={style} contours="off" />
    </CrosshairProvider>
  )
  const { rerender } = render(view(grid, under, 'gradient'))
  expect(perfCounters().renderGrid).toBe(2)

  // Mode: nová měřená mřížka, podklad se nemění
  rerender(view({ ...grid, layers: { call: grid.layers.put, put: grid.layers.call } }, under, 'gradient')) // prettier-ignore
  expect(perfCounters().renderGrid).toBe(3)

  // Nová data dne: nové obě mřížky, tedy oba rastry (invalidace cache)
  const nextGrid = demoGrid(100, 10)
  const nextUnder: HeatmapGrid = { ...nextGrid, layers: { signed: signedOf(9) }, staleAge: null }
  rerender(view(nextGrid, nextUnder, 'gradient'))
  expect(perfCounters().renderGrid).toBe(5)

  // Styl patří oběma vrstvám
  rerender(view(nextGrid, nextUnder, 'blobs'))
  expect(perfCounters().renderGrid).toBe(7)

  // Vypnutý podklad svůj rastr uvolní: po zapnutí se spočte znovu
  rerender(
    <CrosshairProvider>
      <Heatmap grid={nextGrid} underGrid={null} style="blobs" contours="off" />
    </CrosshairProvider>,
  )
  expect(perfCounters().renderGrid).toBe(7)
  rerender(view(nextGrid, nextUnder, 'blobs'))
  expect(perfCounters().renderGrid).toBe(8)
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

test('vlastní čas renderu panelů a profilu: jeden záznam na render (#1419)', () => {
  render(
    <CrosshairProvider>
      <BottomPanels
        data={{
          vol: [100, 200],
          optVolCall: [10, 20],
          optVolPut: [15, 5],
          cumDelta: [50, -100],
          deltaFlowCall: [5, 10],
          deltaFlowPut: [7, 2],
        }}
        visible={{ vol: true, optVol: false, delta: false, deltaFlow: false, evoOi: false, sentiment: false }} // prettier-ignore
        width={400}
      />
      <StrikeProfile
        multiplier={50}
        rows={[
          {
            strike: 7600,
            callVolComponent: 30,
            callOiComponent: 15,
            putVolComponent: 10,
            putOiComponent: 5,
            callVolume: 300,
            putVolume: 100,
            callOi: 1500,
            putOi: 500,
            distanceFromSpot: 0,
          },
        ]}
        spot={7600}
      />
    </CrosshairProvider>,
  )
  expect(perfCounters()).toMatchObject({ renderPanels: 1, renderProfile: 1 })
})
