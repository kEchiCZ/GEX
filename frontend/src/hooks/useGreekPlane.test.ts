/** Test zarovnání řádků plochy na minutovou osu (#204). */
import { describe, expect, it } from 'vitest'
import { alignPlaneProfiles } from './useGreekPlane'
import type { GexProfileRow } from '../replay/loader'

function row(tsIso: string): GexProfileRow {
  return { tsIso, gridStart: 7400, gridStep: 5, values: [1, 2, 3] }
}

describe('alignPlaneProfiles', () => {
  it('mapuje řádky podle ISO času, mimo osu zahazuje, díry nechává null', () => {
    const minutes = [
      '2026-07-30T14:00:00+00:00',
      '2026-07-30T14:01:00+00:00',
      '2026-07-30T14:02:00+00:00',
    ]
    const aligned = alignPlaneProfiles(
      [row(minutes[2]), row(minutes[0]), row('2026-07-30T15:00:00+00:00')],
      minutes,
    )
    expect(aligned).toHaveLength(3)
    expect(aligned[0]?.tsIso).toBe(minutes[0])
    expect(aligned[1]).toBeNull()
    expect(aligned[2]?.tsIso).toBe(minutes[2])
    expect(alignPlaneProfiles([], [])).toEqual([])
  })
})
