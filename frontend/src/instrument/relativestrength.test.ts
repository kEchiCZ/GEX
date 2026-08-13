/** Testy relativní síly (#680): reference od openu, pre-open fallback, spread. */
import { describe, expect, it } from 'vitest'
import { formatPct, relativeStrength } from './relativestrength'
import type { BarRow } from '../api/briefing'

const OPEN_MS = Date.parse('2026-08-13T13:30:00Z')

function bar(ts: string, open: number, close: number): BarRow {
  return { ts_min: ts, open, high: Math.max(open, close), low: Math.min(open, close), close, volume: 1 } // prettier-ignore
}

describe('relativeStrength', () => {
  it('pct od openu prvního baru US seance, spread v pb', () => {
    const es = [
      bar('2026-08-13T10:00:00Z', 6300, 6350), // overnight — nepočítá se
      bar('2026-08-13T13:30:00Z', 6400, 6410),
      bar('2026-08-13T14:00:00Z', 6410, 6432), // +0,5 % od 6400
    ]
    const nq = [bar('2026-08-13T13:30:00Z', 20000, 20100), bar('2026-08-13T14:00:00Z', 20100, 20200)] // prettier-ignore
    const rs = relativeStrength(es, nq, OPEN_MS)
    expect(rs).not.toBeNull()
    expect(rs?.pctA).toBeCloseTo(0.5, 5)
    expect(rs?.pctB).toBeCloseTo(1.0, 5)
    expect(rs?.spreadPb).toBeCloseTo(-0.5, 5) // NQ silnější
    expect(rs?.fromOpen).toBe(true)
  })

  it('před openem se poctivě padá na začátek seance a značí fromOpen=false', () => {
    const es = [bar('2026-08-13T10:00:00Z', 6400, 6432)]
    const nq = [bar('2026-08-13T10:00:00Z', 20000, 20100)]
    const rs = relativeStrength(es, nq, OPEN_MS)
    expect(rs?.fromOpen).toBe(false)
    expect(rs?.pctA).toBeCloseTo(0.5, 5)
  })

  it('prázdné bary jedné strany → null', () => {
    expect(relativeStrength([], [bar('2026-08-13T14:00:00Z', 20000, 20100)], OPEN_MS)).toBeNull()
  })
})

describe('formatPct', () => {
  it('explicitní znaménko a čárka', () => {
    expect(formatPct(0.5)).toBe('+0,50')
    expect(formatPct(-0.456)).toBe('-0,46')
    expect(formatPct(0)).toBe('+0,00')
  })
})
