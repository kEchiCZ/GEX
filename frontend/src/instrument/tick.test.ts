/** Testy cenových ticků: per-symbol tabulka, snap na tick, počet desetin. */
import { expect, test } from 'vitest'
import { priceTick, snapToTick, tickDecimals } from './tick'

test('priceTick: ES/NQ 0,25; jiné dle tabulky; default 0,25', () => {
  expect(priceTick('ES')).toBe(0.25)
  expect(priceTick('NQ')).toBe(0.25)
  expect(priceTick('YM')).toBe(1)
  expect(priceTick('CL')).toBe(0.01)
  expect(priceTick('XYZ')).toBe(0.25) // neznámý → default
})

test('snapToTick zaokrouhlí na nejbližší tick (ES 0,25)', () => {
  expect(snapToTick(7530.13, 0.25)).toBeCloseTo(7530.25, 10)
  expect(snapToTick(7530.1, 0.25)).toBeCloseTo(7530.0, 10)
  expect(snapToTick(7530.375, 0.25)).toBeCloseTo(7530.5, 10)
  expect(snapToTick(7530, 0.25)).toBe(7530)
  expect(snapToTick(123.4, 0)).toBe(123.4) // ochrana proti dělení nulou
})

test('tickDecimals: 0,25→2, 0,1→1, 1→0, 0,01→2', () => {
  expect(tickDecimals(0.25)).toBe(2)
  expect(tickDecimals(0.1)).toBe(1)
  expect(tickDecimals(1)).toBe(0)
  expect(tickDecimals(0.01)).toBe(2)
})
