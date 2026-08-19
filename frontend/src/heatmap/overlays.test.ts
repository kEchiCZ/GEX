/** Extrapolace řádků mimo pásmo strikes (#788) — historie nesmí lhát klampnutím. */
import { describe, expect, it } from 'vitest'

import { candleGeometry, fractionalRow, fractionalRowUnbounded, pricePolyline } from './overlays'

const STRIKES = [6400, 6410, 6420, 6430]

describe('fractionalRowUnbounded', () => {
  it('uvnitř pásma se shoduje s klampující variantou', () => {
    expect(fractionalRowUnbounded(STRIKES, 6415)).toBe(fractionalRow(STRIKES, 6415))
    expect(fractionalRowUnbounded(STRIKES, 6400)).toBe(0)
    expect(fractionalRowUnbounded(STRIKES, 6430)).toBe(3)
  })

  it('mimo pásmo extrapoluje krajním krokem místo klampnutí', () => {
    expect(fractionalRowUnbounded(STRIKES, 6390)).toBe(-1) // 10 b pod pásmem
    expect(fractionalRowUnbounded(STRIKES, 6450)).toBe(5) // 20 b nad pásmem
    // Klampující varianta by obě rozpláclá na okraj — přesně to historie nesmí
    expect(fractionalRow(STRIKES, 6390)).toBe(0)
    expect(fractionalRow(STRIKES, 6450)).toBe(3)
  })
})

describe('cena mimo pásmo v geometrii (#788)', () => {
  const outOfBand = [{ minuteIdx: -5, open: 6480, high: 6490, low: 6470, close: 6485, up: true }]

  it('candleGeometry s unbounded svíčku zachová v extrapolovaných řádcích', () => {
    const candles = candleGeometry(outOfBand, STRIKES, { unbounded: true })
    expect(candles).toHaveLength(1)
    expect(candles[0].highRow).toBeGreaterThan(3) // nad pásmem, ne na okraji
    expect(candles[0].minuteIdx).toBe(-5)
  })

  it('pricePolyline s unbounded drží spojitý průběh mimo pásmo', () => {
    const points = pricePolyline(outOfBand, STRIKES, { unbounded: true })
    expect(points).toHaveLength(1)
    expect(points[0].row).toBeGreaterThan(3)
  })

  it('výchozí (dnešní) chování zůstává klampující — beze změny', () => {
    const candles = candleGeometry(outOfBand, STRIKES)
    expect(candles[0].highRow).toBe(3)
  })
})
