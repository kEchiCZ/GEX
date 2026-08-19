/** Svíčky přes hranici dne (#788): skládání historie do záporných košů. */
import { describe, expect, it } from 'vitest'

import { buildHistoryView, parseHistoryDay, previousDateIso } from './history'
import type { HistoryDay } from './history'

function payloadRow(iso: string, close: number) {
  return { ts_min: iso, open: close - 1, high: close + 2, low: close - 2, close, volume: 10 }
}

function day(date: string, minutes: number, base = 6500): HistoryDay {
  const rows = Array.from({ length: minutes }, (_, index) =>
    payloadRow(`${date}T00:${String(index).padStart(2, '0')}:00+00:00`, base + index),
  )
  const parsed = parseHistoryDay(date, { bars: rows })
  if (parsed === null) throw new Error('testovací den se nesestavil')
  return parsed
}

describe('parseHistoryDay', () => {
  it('řadí bary podle času a čísluje minuteIdx od nuly', () => {
    const parsed = parseHistoryDay('2026-08-18', {
      bars: [payloadRow('2026-08-18T00:01:00+00:00', 6501), payloadRow('2026-08-18T00:00:00+00:00', 6502)], // prettier-ignore
    })
    expect(parsed).not.toBeNull()
    expect(parsed!.bars.map((bar) => bar.minuteIdx)).toEqual([0, 1])
    expect(parsed!.bars[0].close).toBe(6502)
    expect(parsed!.bars[1].up).toBe(false) // 6501 < 6502
  })

  it('prázdná nebo rozbitá odpověď je null (den bez seance)', () => {
    expect(parseHistoryDay('2026-08-16', { bars: [] })).toBeNull()
    expect(parseHistoryDay('2026-08-16', {})).toBeNull()
    expect(parseHistoryDay('2026-08-16', null)).toBeNull()
  })
})

describe('buildHistoryView', () => {
  it('kotví na dnešku: nejnovější den končí košem −1, starší navazuje vlevo', () => {
    const view = buildHistoryView([day('2026-08-18', 10), day('2026-08-17', 10)], 1)
    expect(view.slices).toHaveLength(2)
    const [older, newer] = view.slices
    expect(newer.date).toBe('2026-08-18')
    expect(newer.firstBucket).toBe(-10)
    expect(newer.price.map((bar) => bar.minuteIdx)).toEqual([-10, -9, -8, -7, -6, -5, -4, -3, -2, -1]) // prettier-ignore
    expect(older.date).toBe('2026-08-17')
    expect(older.firstBucket).toBe(-20)
    expect(view.firstBucket).toBe(-20)
  })

  it('dotažení staršího dne NEposune už načtené koše (kotva drží bez kompenzace)', () => {
    const newest = day('2026-08-18', 10)
    const before = buildHistoryView([newest], 1)
    const after = buildHistoryView([newest, day('2026-08-15', 10)], 1)
    const bucketsBefore = before.slices[0].price.map((bar) => bar.minuteIdx)
    const bucketsAfter = after.slices.find((s) => s.date === '2026-08-18')!.price.map((bar) => bar.minuteIdx) // prettier-ignore
    expect(bucketsAfter).toEqual(bucketsBefore)
  })

  it('agreguje do košů timeframe stejně jako dnešní osa', () => {
    const view = buildHistoryView([day('2026-08-18', 10, 6500)], 5)
    expect(view.slices[0].price).toHaveLength(2)
    const [first, second] = view.slices[0].price
    expect(view.firstBucket).toBe(-2)
    expect(first.minuteIdx).toBe(-2)
    expect(second.minuteIdx).toBe(-1)
    // OHLC koše: open z první minuty, close z poslední, high/low přes koš
    expect(first.open).toBe(6499) // open = close − 1 první minuty
    expect(first.close).toBe(6504)
    expect(first.high).toBe(6506)
    expect(second.close).toBe(6509)
  })

  it('bez historie vrací prázdný pohled s kotvou na nule', () => {
    const view = buildHistoryView([], 5)
    expect(view.slices).toEqual([])
    expect(view.firstBucket).toBe(0)
  })
})

describe('previousDateIso', () => {
  it('umí hranici měsíce i roku', () => {
    expect(previousDateIso('2026-08-01')).toBe('2026-07-31')
    expect(previousDateIso('2026-01-01')).toBe('2025-12-31')
    expect(previousDateIso('2026-03-01')).toBe('2026-02-28')
  })
})
