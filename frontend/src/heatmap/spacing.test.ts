/** Testy děravé strike osy (#548): medián rozestupů, cap výšky řádku, pásma děr. */
import { expect, test } from 'vitest'
import { axisGapRanges, capHalfFractions, gapBands, medianStep } from './spacing'

/** NQ vzor z hlášení: klastry 29290–29370 a 29680–29790, díra 310 bodů. */
function gappyAxis(): number[] {
  const strikes: number[] = []
  for (let strike = 29290; strike <= 29370; strike += 10) strikes.push(strike)
  for (let strike = 29680; strike <= 29790; strike += 10) strikes.push(strike)
  return strikes
}

test('medianStep: rovnoměrná osa vrací krok, děravá medián, degenerace 0', () => {
  expect(medianStep([100, 110, 120, 130])).toBe(10)
  expect(medianStep(gappyAxis())).toBe(10) // jedna díra 310 medián nepohne
  expect(medianStep([])).toBe(0)
  expect(medianStep([100])).toBe(0)
})

test('capHalfFractions: plná výška uvnitř klastru, tenká strana k díře (#548)', () => {
  const axis = gappyAxis()
  // Uvnitř klastru: obě poloviny plné
  expect(capHalfFractions(axis, 29330)).toEqual({ up: 1, down: 1 })
  // Kraj díry zdola (29370): nahoru by polovina řádku pokryla 155 bodů,
  // cap = 1.5×10/2 = 7.5 → zlomek 7.5/155; dolů plná
  const below = capHalfFractions(axis, 29370)
  expect(below.up).toBeCloseTo(7.5 / 155, 5)
  expect(below.down).toBe(1)
  // Kraj díry shora (29680) zrcadlově
  const above = capHalfFractions(axis, 29680)
  expect(above.up).toBe(1)
  expect(above.down).toBeCloseTo(7.5 / 155, 5)
  // Okraje osy: extrapolace krajním krokem → plná výška
  expect(capHalfFractions(axis, 29290)).toEqual({ up: 1, down: 1 })
  expect(capHalfFractions(axis, 29790)).toEqual({ up: 1, down: 1 })
})

test('capHalfFractions: rozestup do tolerance je normální, degenerace = plná výška', () => {
  // Mix 10/15 s mediánem 10: 15 ≤ 1.5×10 → žádný cap
  expect(capHalfFractions([100, 110, 125, 135], 110)).toEqual({ up: 1, down: 1 })
  expect(capHalfFractions([100], 100)).toEqual({ up: 1, down: 1 })
  expect(capHalfFractions([], 100)).toEqual({ up: 1, down: 1 })
})

test('gapBands: díra dává pásmo k vymazání, rovnoměrná osa nic (#548)', () => {
  // Osa [100,110,120,430,440]: díra 310 mezi indexy 2 a 3; scaleY 10, offsetY 0
  const bands = gapBands([100, 110, 120, 430, 440], 10, 0)
  expect(bands).toHaveLength(1)
  // Středy řádků: y(3) = (4−3+0.5)·10 = 15, y(2) = 25; capnutá polovina
  // = 5 × (15/310) ≈ 0.242 → pásmo (15.242, 24.758)
  expect(bands[0].top).toBeCloseTo(15 + 5 * (15 / 310), 3)
  expect(bands[0].bottom).toBeCloseTo(25 - 5 * (15 / 310), 3)
  expect(bands[0].bottom).toBeGreaterThan(bands[0].top)
  // Buňka u díry drží nejvýš capnutou polovinu: mazané pásmo začíná
  // pod středem horního řádku a končí nad středem dolního
  expect(bands[0].top).toBeGreaterThan(15)
  expect(bands[0].bottom).toBeLessThan(25)

  expect(gapBands([100, 110, 120, 130], 10, 0)).toEqual([])
  expect(gapBands([100, 110, 120, 135], 10, 0)).toEqual([]) // 15 = 1.5×10 → ještě normální
  expect(gapBands([100, 110, 120, 136], 10, 0)).toHaveLength(1) // 16 > 1.5×10 → díra
  expect(gapBands([], 10, 0)).toEqual([])
})

test('gapBands: offsetY posouvá pásma, scaleY je škáluje', () => {
  const base = gapBands([100, 110, 120, 430, 440], 10, 0)[0]
  const shifted = gapBands([100, 110, 120, 430, 440], 10, 50)[0]
  expect(shifted.top).toBeCloseTo(base.top + 50, 6)
  expect(shifted.bottom).toBeCloseTo(base.bottom + 50, 6)
  const zoomed = gapBands([100, 110, 120, 430, 440], 20, 0)[0]
  expect(zoomed.bottom - zoomed.top).toBeCloseTo(2 * (base.bottom - base.top), 6)
})

test('axisGapRanges: díra nad 2× medián se hlásí jako interval (#548)', () => {
  expect(axisGapRanges(gappyAxis())).toEqual([[29370, 29680]])
  expect(axisGapRanges([100, 110, 120, 130])).toEqual([])
  expect(axisGapRanges([100, 110, 120, 140])).toEqual([]) // 20 = 2×10 → ještě spojité
  expect(axisGapRanges([100, 110, 120, 141])).toEqual([[120, 141]])
  expect(axisGapRanges([])).toEqual([])
})
