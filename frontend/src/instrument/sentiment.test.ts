/** Testy PCR sentimentu (#205): redukce nad snapshot maticí. */
import { expect, test } from 'vitest'
import { formatPcr, pcrAt, pcrVolumeSeries } from './sentiment'
import type { RawDay } from '../heatmap/modes'

function raw(): RawDay {
  // 2 strikes × 3 minuty; index = strikeIdx * minutes + minuteIdx
  return {
    minutes: 3,
    strikes: [7500, 7510],
    callVolume: Float32Array.from([10, 20, 40, 0, 10, 10]),
    putVolume: Float32Array.from([20, 30, 60, 10, 15, 15]),
    callOi: Float32Array.from([100, 100, 100, 100, 100, 100]),
    putOi: Float32Array.from([150, 150, 150, 250, 250, 250]),
    spotSeries: [7505, 7505, 7505],
    staleAge: null,
  }
}

test('pcrAt: poměry přes strikes dané minuty', () => {
  const point = pcrAt(raw(), 2)
  expect(point.volume).toBeCloseTo((60 + 15) / (40 + 10)) // 1.5
  expect(point.oi).toBeCloseTo((150 + 250) / 200) // 2.0
})

test('pcrAt: nulová call strana → null; mimo rozsah → null', () => {
  const data = raw()
  data.callVolume = Float32Array.from([0, 0, 0, 0, 0, 0])
  expect(pcrAt(data, 0).volume).toBeNull()
  expect(pcrAt(raw(), 99)).toEqual({ volume: null, oi: null })
  expect(pcrAt({ ...raw(), minutes: 0, strikes: [] }, 0)).toEqual({ volume: null, oi: null })
})

test('pcrVolumeSeries: denní řada pro sparkline', () => {
  const series = pcrVolumeSeries(raw())
  expect(series).toHaveLength(3)
  expect(series[0]).toBeCloseTo(30 / 10) // minuta 0: put (20+10) / call (10+0)
  expect(series[2]).toBeCloseTo(1.5)
})

test('formatPcr', () => {
  expect(formatPcr(1.234)).toBe('1.23')
  expect(formatPcr(null)).toBe('—')
})
