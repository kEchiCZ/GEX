/** Test formátování chipu gamma útesu (#576). */
import { expect, test } from 'vitest'
import { formatCliffShare } from './GammaCliffChip'

test('formatCliffShare: procenta, null a nečíslo', () => {
  expect(formatCliffShare(0.6)).toBe('60 %')
  expect(formatCliffShare(0.154)).toBe('15 %')
  expect(formatCliffShare(null)).toBeNull()
  expect(formatCliffShare(Number.NaN)).toBeNull()
})
