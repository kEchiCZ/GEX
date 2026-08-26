/** Testy kalkulačky pozice (#679): floor kontraktů, micro varianta, meze. */
import { describe, expect, it } from 'vitest'
import { positionLabel, positionSize, stopVsRange } from './position'

describe('positionSize', () => {
  it('ES: riziko 1 % z 5 000 $ = 50 $, stop 8 b → 0× ES, 1× MES', () => {
    const size = positionSize({ symbol: 'ES', entry: 6400, stop: 6392, accountUsd: 5000, riskPct: 1 }) // prettier-ignore
    expect(size).not.toBeNull()
    expect(size?.riskUsd).toBe(50)
    expect(size?.stopPoints).toBe(8)
    expect(size?.contracts).toBe(0) // 50 / (8 × 50) = 0,125 → floor 0
    expect(size?.micro).toEqual({ symbol: 'MES', contracts: 1 }) // 50 / (8 × 5) = 1,25
  })

  it('větší účet unese plný kontrakt', () => {
    const size = positionSize({ symbol: 'ES', entry: 6400, stop: 6395, accountUsd: 50_000, riskPct: 1 }) // prettier-ignore
    expect(size?.contracts).toBe(2) // 500 / (5 × 50) = 2
    expect(size?.micro?.contracts).toBe(20)
  })

  it('NQ mapuje na MNQ', () => {
    const size = positionSize({ symbol: 'NQ', entry: 20000, stop: 19980, accountUsd: 10_000, riskPct: 2 }) // prettier-ignore
    expect(size?.contracts).toBe(0) // 200 / (20 × 20) = 0,5
    expect(size?.micro).toEqual({ symbol: 'MNQ', contracts: 5 })
  })

  it('symbol bez micro varianty → micro null', () => {
    const size = positionSize({ symbol: 'CL', entry: 80, stop: 79.5, accountUsd: 50_000, riskPct: 1 }) // prettier-ignore
    expect(size?.micro).toBeNull()
  })

  it('nulový stop / účet / riziko → null (nedělit nulou, nekreslit nesmysl)', () => {
    expect(positionSize({ symbol: 'ES', entry: 6400, stop: 6400, accountUsd: 5000, riskPct: 1 })).toBeNull() // prettier-ignore
    expect(positionSize({ symbol: 'ES', entry: 6400, stop: 6390, accountUsd: 0, riskPct: 1 })).toBeNull() // prettier-ignore
    expect(positionSize({ symbol: 'ES', entry: 6400, stop: 6390, accountUsd: 5000, riskPct: 0 })).toBeNull() // prettier-ignore
  })
})

describe('positionLabel', () => {
  it('kompaktní řádek s rizikem, stopem a kontrakty', () => {
    const size = positionSize({ symbol: 'ES', entry: 6400, stop: 6392, accountUsd: 5000, riskPct: 1 }) // prettier-ignore
    expect(positionLabel(size!, 1)).toBe('riziko 50 $ (1 %) · stop 8 b → ES 0× · MES 1×')
  })
})

describe('stopVsRange (#874)', () => {
  const vol = (bucket: string, range = 42) => ({ bucket, session_range: range })

  it('podíl stopu na typickém rozsahu + caution pro elevated/crisis', () => {
    expect(stopVsRange(8, vol('normal'))).toEqual({ share: 8 / 42, caution: false })
    expect(stopVsRange(8, vol('elevated'))).toEqual({ share: 8 / 42, caution: true })
    expect(stopVsRange(8, vol('crisis'))).toEqual({ share: 8 / 42, caution: true })
    expect(stopVsRange(8, vol('low'))).toEqual({ share: 8 / 42, caution: false })
  })

  it('bez vol režimu nebo nesmyslných vstupů → null (žádný default, ADR-0028)', () => {
    expect(stopVsRange(8, null)).toBeNull()
    expect(stopVsRange(8, vol('normal', 0))).toBeNull()
    expect(stopVsRange(0, vol('normal'))).toBeNull()
  })
})
