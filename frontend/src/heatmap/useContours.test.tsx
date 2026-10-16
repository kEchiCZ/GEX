/** Hook kontur (#493): sync fallback bez Workeru, multi-slot cache, parita výpočtu. */
import { renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import {
  computeContourPolylines,
  computeContourSegments,
  flatToSegments,
  segmentsToFlat,
} from './contourCompute'
import type { HeatmapGrid } from './grid'
import { useContours } from './useContours'

function makeGrid(seed = 1): HeatmapGrid {
  const minutes = 20
  const strikes = [7500, 7505, 7510, 7515, 7520, 7525, 7530, 7535]
  const signed = new Float32Array(minutes * strikes.length)
  for (let index = 0; index < signed.length; index += 1) {
    // Deterministický kopec s oběma znaménky — ať vzniknou kladné i záporné kontury
    signed[index] = Math.sin((index * seed) / 7) * (index % 13 === 0 ? -1 : 1)
  }
  return { minutes, strikes, layers: { signed }, staleAge: null } as unknown as HeatmapGrid
}

describe('useContours (jsdom = sync fallback, Worker není)', () => {
  it('vrací segmenty shodné s přímým výpočtem', () => {
    const grid = makeGrid()
    const { result } = renderHook(() => useContours(grid, null, 'all'))
    const direct = computeContourPolylines(
      grid.layers.signed as Float32Array,
      grid.minutes,
      grid.strikes.length,
      'all',
    )
    expect(result.current.levels.length).toBe(direct.length)
    expect(result.current.levels.slice(0, 3)).toEqual(direct.slice(0, 3))
    expect(result.current.levels.length).toBeGreaterThan(0)
    expect(result.current.flip).toEqual([]) // 'all' bez flipu
  })

  it('hladiny i flip (#1420): týž výsledek jako samostatné výpočty', () => {
    const grid = makeGrid(4)
    const { result } = renderHook(() => useContours(grid, null, 'major+flip'))
    const field = grid.layers.signed as Float32Array
    const size = [field, grid.minutes, grid.strikes.length] as const
    expect(result.current.levels).toEqual(computeContourPolylines(...size, 'major'))
    expect(result.current.flip).toEqual(computeContourPolylines(...size, 'flip'))
    expect(result.current.flip.length).toBeGreaterThan(0)
  })

  it('cache: přepnutí módu tam a zpět vrátí IDENTICKÉ pole (žádný nový výpočet)', () => {
    const grid = makeGrid(2)
    const { result, rerender } = renderHook(({ mode }) => useContours(grid, null, mode), {
      initialProps: { mode: 'all' as const },
    })
    const first = result.current.levels
    rerender({ mode: 'major' as never })
    rerender({ mode: 'all' as never })
    expect(result.current.levels).toBe(first) // stejná reference = cache hit (AC 2)
  })

  it('off → prázdno; flat kódování je bezeztrátové', () => {
    const grid = makeGrid(3)
    const { result } = renderHook(() => useContours(grid, null, 'off'))
    expect(result.current).toEqual({ levels: [], flip: [] })
    const direct = computeContourSegments(
      grid.layers.signed as Float32Array,
      grid.minutes,
      grid.strikes.length,
      'major',
    )
    // Float32 zaokrouhlení při přenosu je pod rozlišením kreslení (< 1e-4 buňky)
    const roundtrip = flatToSegments(segmentsToFlat(direct))
    expect(roundtrip.length).toBe(direct.length)
    roundtrip.forEach((segment, index) =>
      segment.forEach((value, part) => expect(value).toBeCloseTo(direct[index][part], 4)),
    )
  })
})
