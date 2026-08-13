/** Testy presetů rozsahů (#487): DST, flip cross, klouzavé okno, disabled stavy. */
import { describe, expect, it } from 'vitest'
import { lastFlipCrossIndex, presetRange } from './rangepresets'
import type { PresetInputs } from './rangepresets'

/** 1m osa od `startIso` o `count` minutách. */
function axis(startIso: string, count: number): string[] {
  const start = Date.parse(startIso)
  return Array.from({ length: count }, (_, idx) => new Date(start + idx * 60_000).toISOString())
}

function inputs(overrides: Partial<PresetInputs>): PresetInputs {
  // Letní seance 13. 8.: osa od 22:00 UTC (12. 8.) — Globex začátek;
  // US open 13:30 UTC (EDT), 1170 minut = do 17:30 UTC
  const minutesIso = axis('2026-08-12T22:00:00Z', 1170)
  return {
    dateIso: '2026-08-13',
    minutesIso,
    spotSeries: minutesIso.map(() => 6400),
    flipSeries: minutesIso.map(() => null),
    positionIdx: minutesIso.length - 1,
    ...overrides,
  }
}

describe('presety přes DST (AC)', () => {
  it('US open +30 min v létě = 13:30–14:00 UTC (EDT)', () => {
    const range = presetRange('open30', inputs({}))
    expect(range).toEqual({
      fromIso: '2026-08-13T13:30:00.000Z',
      toIso: '2026-08-13T14:00:00.000Z',
    })
  })

  it('US open v zimě = 14:30 UTC (EST)', () => {
    const minutesIso = axis('2026-01-14T23:00:00Z', 1170)
    const range = presetRange('open30', inputs({ dateIso: '2026-01-15', minutesIso, positionIdx: 1169 })) // prettier-ignore
    expect(range?.fromIso).toBe('2026-01-15T14:30:00.000Z')
  })

  it('RTH = open → close (16:00 ET), t2 clamp na pozici dat', () => {
    const range = presetRange('rth', inputs({}))
    expect(range?.fromIso).toBe('2026-08-13T13:30:00.000Z')
    // Osa končí 17:30 UTC — dřív než 20:00 UTC close → clamp
    expect(range?.toIso).toBe('2026-08-13T17:29:00.000Z')
  })

  it('před US openem jsou open30/RTH disabled (null)', () => {
    expect(presetRange('open30', inputs({ positionIdx: 60 }))).toBeNull()
    expect(presetRange('rth', inputs({ positionIdx: 60 }))).toBeNull()
  })
})

describe('globex noc', () => {
  it('začátek osy → poslední minuta před openem', () => {
    const range = presetRange('globex', inputs({}))
    expect(range?.fromIso).toBe('2026-08-12T22:00:00.000Z')
    expect(range?.toIso).toBe('2026-08-13T13:29:00.000Z')
  })

  it('uprostřed noci končí na pozici', () => {
    const range = presetRange('globex', inputs({ positionIdx: 120 }))
    expect(range?.toIso).toBe('2026-08-13T00:00:00.000Z')
  })
})

describe('posledních 30 min (klouzavé)', () => {
  it('okno 30 minut končící na pozici', () => {
    const range = presetRange('last30', inputs({ positionIdx: 500 }))
    expect(range?.fromIso).toBe(axis('2026-08-12T22:00:00Z', 1170)[471])
    expect(range?.toIso).toBe(axis('2026-08-12T22:00:00Z', 1170)[500])
  })

  it('na začátku dne se zkrátí k první minutě', () => {
    const range = presetRange('last30', inputs({ positionIdx: 10 }))
    expect(range?.fromIso).toBe('2026-08-12T22:00:00.000Z')
  })
})

describe('od flip crossu (AC: proti levels řadě)', () => {
  it('najde poslední změnu znaménka spot − flip', () => {
    const spot = [6390, 6395, 6405, 6410, 6398, 6395, 6402]
    const flip = [6400, 6400, 6400, 6400, 6400, 6400, 6400]
    // Crossy: idx 2 (pod→nad), idx 4 (nad→pod), idx 6 (pod→nad) — poslední 6
    expect(lastFlipCrossIndex(spot, flip, 6)).toBe(6)
    expect(lastFlipCrossIndex(spot, flip, 5)).toBe(4)
  })

  it('díry (null) nevyrábí falešný cross; bez crossu null → disabled', () => {
    const spot = [6390, null, 6395, 6396]
    const flip = [6400, 6400, null, 6400]
    expect(lastFlipCrossIndex(spot, flip, 3)).toBeNull()
    expect(presetRange('flipcross', inputs({}))).toBeNull() // flip řada samé null
  })

  it('preset vrací okno cross → pozice', () => {
    const minutesIso = axis('2026-08-12T22:00:00Z', 10)
    const spot = [6390, 6390, 6405, 6405, 6405, 6405, 6405, 6405, 6405, 6405]
    const flip = minutesIso.map(() => 6400)
    const range = presetRange('flipcross', inputs({ minutesIso, spotSeries: spot, flipSeries: flip, positionIdx: 8 })) // prettier-ignore
    expect(range?.fromIso).toBe(minutesIso[2])
    expect(range?.toIso).toBe(minutesIso[8])
  })
})
