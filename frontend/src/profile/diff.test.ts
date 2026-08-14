/** Golden test diferenčního profilu (#489): ruční příklad B − A. */
import { describe, expect, it } from 'vitest'
import { diffPeak, diffProfileRows } from './diff'
import type { ProfileRow } from './bars'

function row(strike: number, callVol: number, putVol: number): ProfileRow {
  return {
    strike,
    callVolComponent: 0,
    callOiComponent: 0,
    putVolComponent: 0,
    putOiComponent: 0,
    callVolume: callVol,
    putVolume: putVol,
    callOi: 0,
    putOi: 0,
    distanceFromSpot: 0,
  }
}

describe('diffProfileRows (#489)', () => {
  it('golden: ruční příklad B − A per strike a strana', () => {
    // Okno A (pre): 6400 C50/P30, 6410 C20/P40
    // Okno B (post): 6400 C80/P10, 6420 C15/P5 (6410 v B bez aktivity)
    const a = [row(6400, 50, 30), row(6410, 20, 40)]
    const b = [row(6400, 80, 10), row(6420, 15, 5)]
    expect(diffProfileRows(a, b)).toEqual([
      { strike: 6400, callDelta: 30, putDelta: -20 }, // 80−50, 10−30
      { strike: 6410, callDelta: -20, putDelta: -40 }, // chybí v B → 0−vol_A
      { strike: 6420, callDelta: 15, putDelta: 5 }, // chybí v A → vol_B−0
    ])
  })

  it('prázdná strana / prázdné vstupy', () => {
    expect(diffProfileRows([], [])).toEqual([])
    expect(diffProfileRows([], [row(6400, 10, 5)])).toEqual([
      { strike: 6400, callDelta: 10, putDelta: 5 },
    ])
  })

  it('diffPeak = max |hodnota| napříč stranami', () => {
    const rows = diffProfileRows([row(6400, 50, 30)], [row(6400, 80, 0)])
    expect(diffPeak(rows)).toBe(30)
    expect(diffPeak([])).toBe(0)
  })
})
