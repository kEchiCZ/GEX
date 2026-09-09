/** Trend napříč timeframy (#1089): pivoty, struktura, EMA, verdikt TF a čtení shora dolů. */
import { describe, expect, test } from 'vitest'
import {
  MIN_CANDLES,
  assessTimeframe,
  assessTrends,
  ema,
  expectedDirection,
  findPivots,
  readEma,
  readStructure,
} from './trend'
import type { Candle } from './trend'

function candle(close: number, index: number, spread = 1): Candle {
  return {
    ts: new Date(Date.UTC(2026, 0, 1) + index * 3_600_000).toISOString(),
    open: close,
    high: close + spread,
    low: close - spread,
    close,
    volume: 100,
  }
}

/** Zigzag řada: trend `step` per svíčku, vlny o `amplitude` (> 4·step, ať swingy přežijí trend). */
function zigzag(count: number, step: number, amplitude = 12, wave = 8): Candle[] {
  const rows: Candle[] = []
  for (let i = 0; i < count; i += 1) {
    const phase = (i % wave) / wave
    const swing = phase < 0.5 ? phase * 2 : (1 - phase) * 2 // 0→1→0
    rows.push(candle(1000 + step * i + swing * amplitude, i))
  }
  return rows
}

describe('ema', () => {
  test('začíná prostým průměrem a sleduje řadu', () => {
    const values = Array.from({ length: 30 }, (_, i) => i + 1)
    const result = ema(values, 20)
    expect(result).toHaveLength(11)
    expect(result[0]).toBeCloseTo(10.5)
    expect(result[10]).toBeGreaterThan(result[0])
    expect(ema([1, 2, 3], 5)).toEqual([])
  })
})

describe('findPivots + readStructure', () => {
  test('rostoucí zigzag dává HH/HL', () => {
    const rows = zigzag(40, 1)
    const pivots = findPivots(rows, 3)
    expect(pivots.some((p) => p.kind === 'high')).toBe(true)
    expect(pivots.some((p) => p.kind === 'low')).toBe(true)
    expect(readStructure(pivots)?.kind).toBe('HH/HL')
  })

  test('klesající zigzag dává LH/LL', () => {
    expect(readStructure(findPivots(zigzag(40, -1), 3))?.kind).toBe('LH/LL')
  })

  test('boční zigzag = mixed; bez dvou swingů null', () => {
    expect(readStructure(findPivots(zigzag(40, 0), 3))?.kind).toBe('mixed')
    expect(readStructure(findPivots(zigzag(6, 1), 3))).toBeNull()
  })
})

describe('readEma', () => {
  test('cena nad rostoucí EMA20 nad EMA50 = up; pod = down; jinak range', () => {
    const up = readEma(zigzag(60, 2))
    expect(up?.direction).toBe('up')
    expect(up?.fastAboveSlow).toBe(true)
    expect(up?.slope).toBeGreaterThan(0)
    expect(readEma(zigzag(60, -2))?.direction).toBe('down')
    expect(readEma(zigzag(60, 0))?.direction).toBe('range')
    expect(readEma(zigzag(10, 1))).toBeNull()
  })

  test('pod 50 svíček chybí EMA50, směr se čte jen z EMA20', () => {
    const reading = readEma(zigzag(30, 2))
    expect(reading?.ema50).toBeNull()
    expect(reading?.fastAboveSlow).toBeNull()
    expect(reading?.direction).toBe('up')
  })
})

describe('assessTimeframe', () => {
  test('málo dat → bez verdiktu a poctivá poznámka', () => {
    const result = assessTimeframe('D', zigzag(5, 1))
    expect(result.direction).toBeNull()
    expect(result.note).toBe(`málo dat (5/${MIN_CANDLES} svíček)`)
  })

  test('struktura i EMA souhlasí = silný trend', () => {
    const result = assessTimeframe('D', zigzag(60, 2))
    expect(result.direction).toBe('up')
    expect(result.strength).toBe('strong')
    expect(result.note).toContain('struktura HH/HL')
    expect(result.note).toContain('cena nad EMA20, EMA20 nad EMA50')
  })

  test('rozdělaná svíčka se do struktury nepočítá', () => {
    const rows = zigzag(60, -2)
    rows[rows.length - 1] = { ...rows[rows.length - 1], high: 5000, partial: true }
    const result = assessTimeframe('60', rows)
    expect(result.structure?.kind).toBe('LH/LL')
  })
})

describe('assessTrends', () => {
  test('souhlas napříč TF: long ve směru, čtení to říká', () => {
    const report = assessTrends({
      W: zigzag(60, 2),
      D: zigzag(60, 2),
      '240': zigzag(60, 2),
      '60': zigzag(60, 2),
      '15': zigzag(60, 2),
    })
    expect(report.higher).toBe('up')
    expect(report.lower).toBe('up')
    expect(report.aligned).toBe(5)
    expect(report.decided).toBe(5)
    expect(report.expected).toBe('up')
    expect(report.reading).toContain('obchodovat long ve směru trendu')
  })

  test('nižší TF proti dennímu = korekce; očekávání zůstává u vyššího TF', () => {
    const report = assessTrends({
      W: zigzag(60, 2),
      D: zigzag(60, 2),
      '240': zigzag(60, -2),
      '60': zigzag(60, -2),
      '15': zigzag(60, -2),
    })
    expect(report.higher).toBe('up')
    expect(report.lower).toBe('down')
    expect(report.expected).toBe('up')
    expect(report.reading).toContain('korigují proti vyššímu')
  })

  test('týden proti dni: den rozhoduje, čtení označí korekci', () => {
    const report = assessTrends({ W: zigzag(60, 2), D: zigzag(60, -2), '240': zigzag(60, -2) })
    expect(report.higher).toBe('down')
    expect(report.reading).toContain('denní trend je korekce uvnitř týdenního')
  })

  test('týden se směrem, den bez trendu = konsolidace; bez převahy', () => {
    const report = assessTrends({ W: zigzag(60, 2), D: zigzag(60, 0) })
    expect(report.expected).toBeNull()
    expect(report.reading).toContain('konsolidace uvnitř týdenního trendu')
    expect(report.reading).toContain('Vyšší TF bez trendu')
  })

  test('vyšší TF bez trendu → bez převahy', () => {
    const report = assessTrends({ W: zigzag(60, 0), D: zigzag(60, 0), '60': zigzag(60, 2) })
    expect(report.expected).toBeNull()
    expect(report.reading).toContain('Vyšší TF bez trendu')
  })

  test('bez dat vůbec', () => {
    const report = assessTrends({})
    expect(report.decided).toBe(0)
    expect(report.reading).toBe('Zatím málo svíček pro čtení trendu.')
    expect(report.expected).toBeNull()
  })
})

describe('expectedDirection', () => {
  test('vyšší rozhoduje; bez vyššího přebírá nižší jen se směrem', () => {
    expect(expectedDirection('up', 'down')).toBe('up')
    expect(expectedDirection('range', 'up')).toBeNull()
    expect(expectedDirection(null, 'down')).toBe('down')
    expect(expectedDirection(null, 'range')).toBeNull()
  })
})
