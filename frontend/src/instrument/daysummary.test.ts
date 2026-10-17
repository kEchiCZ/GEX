/** Shrnutí dne (#1090): úrovně obratu, očekávaná reakce zpráv, popisky verdiktu dne. */
import { describe, expect, test } from 'vitest'
import {
  VERDICT_LABELS,
  newsExpectations,
  pragueTime,
  turnLevels,
  verdictSummary,
} from './daysummary'
import type { NewsRow } from '../api/news'

function newsRow(overrides: Partial<NewsRow>): NewsRow {
  return {
    id: 1,
    ts_event: '2026-09-09T12:30:00Z',
    ts_ingested: '2026-09-09T00:00:00Z',
    source: 'forexfactory',
    kind: 'scheduled',
    category: 'MACRO_INFLATION',
    importance: 3,
    raw: { impact: 'High' },
    title: 'USD CPI m/m',
    summary: null,
    sentiment_dir: null,
    sentiment_score: null,
    sentiment_source: null,
    forecast: 0.3,
    previous: 0.2,
    actual: null,
    ...overrides,
  }
}

describe('turnLevels', () => {
  test('řadí podle vzdálenosti, přiděluje roli a konfluenci', () => {
    const levels = turnLevels({
      price: 7600,
      levels: { ts_min: '', flip: 7580, call_wall: 7650, put_wall: 7500, centroid: 7590, total_gex: 1 }, // prettier-ignore
      reference: { onHigh: 7612, onLow: 7570, onRunning: true, prevHigh: 7651, prevLow: 7520, prevClose: 7595, vwap: [] }, // prettier-ignore
      em: { em: 40, anchor: 7600 },
      dailyEma20: 7560,
    })
    expect(levels[0].label).toBe('PDC')
    expect(levels[0].role).toBe('podpora')
    expect(levels.map((row) => row.distance)).toEqual([...levels.map((row) => row.distance)].sort((a, b) => a - b)) // prettier-ignore
    const callWall = levels.find((row) => row.label === 'Call wall')!
    expect(callWall.role).toBe('odpor')
    expect(callWall.confluence).toEqual(['PDH']) // 7651 do 0,1 % od 7650, +EM 7640 už ne
    expect(levels.find((row) => row.label === 'ONH (běží)')).toBeDefined()
    expect(levels.find((row) => row.label === '+EM')?.price).toBe(7640)
  })

  test('cena úrovně se zaokrouhlí na setiny', () => {
    const [flip] = turnLevels({ price: 29500, levels: { ts_min: '', flip: 29496.89431806286, call_wall: null, put_wall: null, centroid: null, total_gex: 1 }, reference: null, em: null, dailyEma20: null }) // prettier-ignore
    expect(flip.price).toBe(29496.89)
  })

  test('bez ceny nebo bez vstupů prázdný seznam', () => {
    expect(turnLevels({ price: null, levels: null, reference: null, em: null, dailyEma20: null })).toEqual([]) // prettier-ignore
    expect(turnLevels({ price: 7600, levels: null, reference: null, em: null, dailyEma20: null })).toEqual([]) // prettier-ignore
  })
})

describe('newsExpectations', () => {
  const usOpen = Date.parse('2026-09-09T13:30:00Z')
  const now = Date.parse('2026-09-09T08:00:00Z')

  test('čas v Praze, směr z konvence, velikost z měřených reakcí', () => {
    const [cpi] = newsExpectations(
      [newsRow({ series_sign: -1 })],
      [{ category: 'MACRO_INFLATION', windows: { '5': { median_abs_bp: 15.4, n: 12 }, '15': { median_abs_bp: 22, n: 10 } } }], // prettier-ignore
      usOpen,
      now,
    )
    expect(cpi.timeLabel).toBe('14:30') // 12:30 UTC = 14:30 CEST
    expect(cpi.direction).toBe('nad konsensem = risk-off, pod = risk-on')
    expect(cpi.magnitude).toBe('typicky ±15 bp/5 min (n=12), ±22 bp/15 min (n=10)')
    expect(cpi.highImpact).toBe(true)
    expect(cpi.beforeOpen).toBe(true)
  })

  test('bez konvence a bez měření se nic nedosazuje; High-impact napřed', () => {
    const rows = newsExpectations(
      [
        newsRow({ id: 2, title: 'Fed speech', category: 'FED', raw: { impact: 'Low' }, importance: 1, ts_event: '2026-09-09T09:00:00Z' }), // prettier-ignore
        newsRow({ id: 3, ts_event: '2026-09-09T15:00:00Z' }),
        newsRow({ id: 4, title: 'Bond Auction', category: 'OTHER', raw: { impact: 'Low' }, importance: 1, ts_event: '2026-09-09T10:00:00Z' }), // prettier-ignore
      ],
      [{ category: 'OTHER', windows: { '5': { median_abs_bp: 3, n: 4499 } } }],
      usOpen,
      now,
    )
    expect(rows[0].row.id).toBe(3)
    expect(rows[1].direction).toBe('směr překvapení bez konvence řady')
    expect(rows[1].magnitude).toBe('bez měřené reakce')
    expect(rows[0].beforeOpen).toBe(false) // po openu
    // Kategorie OTHER má tisíce měření, ale medián přes „ostatní" nic neříká
    expect(rows[2].magnitude).toBe('bez měřené reakce')
  })

  test('pragueTime respektuje zimní čas', () => {
    expect(pragueTime('2026-12-10T13:30:00Z')).toBe('14:30')
  })
})

describe('verdictSummary (#1595): věta uloženého verdiktu enginu', () => {
  test('popisek a věta podle verdiktu, skóre se znaménkem', () => {
    expect(VERDICT_LABELS.long).toBe('Spíše LONG den')
    expect(verdictSummary('long', 5)).toContain('Spíše long den (skóre +5)')
    expect(verdictSummary('short', -4)).toContain('Spíše short den (skóre -4)')
    expect(verdictSummary('none', 0)).toContain('Bez převahy (skóre +0)')
    expect(verdictSummary('wait_news', 8)).toContain('platí až po tisku')
  })
})
