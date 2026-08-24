/** Gamma pole pro budoucí okamžiky (#834). */
import { expect, test } from 'vitest'
import { TAU_FLOOR_S, bsGamma, fieldAt, secondsToSettleSeries } from './gammaforward'

test('gamma je maximální na strike a s ubývajícím časem se zužuje (#834)', () => {
  const strike = 7700
  const iv = 0.2
  const farS = 6 * 3600
  const nearS = 900

  // Na penězích je gamma vždy nejvyšší
  const atmFar = bsGamma(strike, strike, iv, farS)
  expect(atmFar).toBeGreaterThan(bsGamma(strike + 50, strike, iv, farS))

  // Jádro podstaty issue: blíž k expiraci je ATM gamma VYŠŠÍ a křídla NIŽŠÍ —
  // pole se sevře. Konstantní projekce tenhle tvar zamrazí.
  expect(bsGamma(strike, strike, iv, nearS)).toBeGreaterThan(atmFar)
  expect(bsGamma(strike + 50, strike, iv, nearS)).toBeLessThan(
    bsGamma(strike + 50, strike, iv, farS),
  )
})

test('gamma je nulová pro nesmyslné vstupy místo NaN', () => {
  expect(bsGamma(0, 7700, 0.2, 3600)).toBe(0)
  expect(bsGamma(7700, 0, 0.2, 3600)).toBe(0)
  expect(bsGamma(7700, 7700, 0, 3600)).toBe(0)
  // Podlaha τ: v okamžiku expirace by gamma divergovala
  expect(Number.isFinite(bsGamma(7700, 7700, 0.2, 0))).toBe(true)
})

test('fieldAt: call kladně, put záporně, prázdné buňky se přeskočí', () => {
  const grid = [7650, 7700, 7750]
  const call = fieldAt(grid, [{ strike: 7700, signedOi: 100, iv: 0.2 }], 3600, 50)
  const put = fieldAt(grid, [{ strike: 7700, signedOi: -100, iv: 0.2 }], 3600, 50)

  expect(call[1]).toBeGreaterThan(0)
  expect(put[1]).toBeLessThan(0)
  expect(call[1]).toBeCloseTo(-put[1], 10)
  // Maximum na striku, ne na krajích mřížky
  expect(call[1]).toBeGreaterThan(call[0])
  expect(call[1]).toBeGreaterThan(call[2])

  // Kontrakty bez OI nebo bez IV do pole nepřispívají (díra, ne výmysl)
  const empty = fieldAt(grid, [{ strike: 7700, signedOi: 0, iv: 0.2 }], 3600, 50)
  expect(Array.from(empty)).toEqual([0, 0, 0])
  const noIv = fieldAt(grid, [{ strike: 7700, signedOi: 100, iv: 0 }], 3600, 50)
  expect(Array.from(noIv)).toEqual([0, 0, 0])
})

test('secondsToSettleSeries klesá po koších a nespadne pod podlahu τ', () => {
  const last = Date.UTC(2026, 7, 24, 19, 0)
  const settle = Date.UTC(2026, 7, 24, 20, 0)

  const series = secondsToSettleSeries(last, settle, 4, 15)

  expect(series).toEqual([45 * 60, 30 * 60, 15 * 60, TAU_FLOOR_S])
  // Sloupce za settle drží podlahu, ne záporný čas
  expect(secondsToSettleSeries(last, settle, 6, 15).slice(4)).toEqual([TAU_FLOOR_S, TAU_FLOOR_S])
})
