/** Shrnutí dne (#1090): úrovně obratu, očekávaná reakce zpráv, verdikt dne. */
import { describe, expect, test } from 'vitest'
import {
  VERDICT_RULES_VERSION,
  dayVerdict,
  newsExpectations,
  pragueTime,
  turnLevels,
} from './daysummary'
import type { VerdictInput } from './daysummary'
import type { NewsRow } from '../api/news'
import type { TrendReport } from './trend'

function trendReport(higher: TrendReport['higher'], lower: TrendReport['lower']): TrendReport {
  return {
    byTimeframe: [],
    higher,
    lower,
    aligned: 0,
    decided: 0,
    reading: '',
    expected: higher === 'range' ? null : higher,
  }
}

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
    expect(callWall.confluence).toEqual(['PDH', '+EM']) // 7651 a 7640 do 0,25 % od 7650
    expect(levels.find((row) => row.label === 'ONH (běží)')).toBeDefined()
    expect(levels.find((row) => row.label === '+EM')?.price).toBe(7640)
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
      ],
      [],
      usOpen,
      now,
    )
    expect(rows[0].row.id).toBe(3)
    expect(rows[1].direction).toBe('směr překvapení bez konvence řady')
    expect(rows[1].magnitude).toBe('bez měřené reakce')
    expect(rows[0].beforeOpen).toBe(false) // po openu
  })

  test('pragueTime respektuje zimní čas', () => {
    expect(pragueTime('2026-12-10T13:30:00Z')).toBe('14:30')
  })
})

describe('dayVerdict', () => {
  const base: VerdictInput = {
    trend: trendReport('up', 'up'),
    positiveGamma: false,
    tendencyBand: 'long',
    sentiment: { symbol: 'ES', state: 'RiskOn', unconfirmed: false, unconfirmed_state: '', last_close: null, ma5: null, ma10: null, threshold: null, current_wave: { direction: '' } } as never, // prettier-ignore
    price: 7610,
    prevClose: 7600,
    oiDelta: { symbol: 'ES', expiry: '', days: { current: 'd', previous: 'p' }, call_total: 1000, put_total: 800, call_delta: 300, put_delta: 50 }, // prettier-ignore
    newsBeforeOpen: false,
  }

  test('souhlas všech složek = spíše long; každý hlas má důvod', () => {
    const result = dayVerdict(base)
    // trend 2+1, tendence 1, sentiment 1, overnight 1, ΔOI 1, gamma +1 = 8
    expect(result.score).toBe(8)
    expect(result.verdict).toBe('long')
    expect(result.votes.map((vote) => vote.name)).toEqual(['trend_higher', 'trend_lower', 'tendency', 'sentiment', 'overnight', 'oi_delta', 'gamma']) // prettier-ignore
    expect(result.votes.every((vote) => vote.reason.length > 0)).toBe(true)
    expect(result.summary).toContain('Spíše long den (skóre +8)')
    expect(VERDICT_RULES_VERSION).toBe(1)
  })

  test('pozitivní gamma táhne skóre k nule; práh ±3', () => {
    const weak = dayVerdict({
      ...base,
      positiveGamma: true,
      tendencyBand: 'neutral',
      sentiment: null,
      oiDelta: null,
    })
    // trend 2+1, overnight 1 = 4 → gamma −1 = 3 → long na hraně
    expect(weak.score).toBe(3)
    expect(weak.verdict).toBe('long')
    const none = dayVerdict({
      ...base,
      positiveGamma: true,
      tendencyBand: 'neutral',
      sentiment: null,
      oiDelta: null,
      trend: trendReport('up', 'range'),
    })
    expect(none.score).toBe(2) // 2 + 0 + overnight 1 − gamma 1
    expect(none.verdict).toBe('none')
  })

  test('short zrcadlově; nepotvrzený sentiment nehlasuje', () => {
    const result = dayVerdict({
      ...base,
      trend: trendReport('down', 'down'),
      tendencyBand: 'strong_short',
      sentiment: { ...(base.sentiment as object), state: 'RiskOff', unconfirmed: true } as never,
      price: 7590,
      oiDelta: { ...base.oiDelta!, call_delta: 20, put_delta: 400 },
    })
    // −2 −1 −2 +0 −1 −1 gamma −1 = −8
    expect(result.score).toBe(-8)
    expect(result.verdict).toBe('short')
    expect(result.votes.find((vote) => vote.name === 'sentiment')?.reason).toContain('nepotvrzený')
  })

  test('High-impact zpráva před openem = počkat na tisk, skóre zůstává', () => {
    const result = dayVerdict({ ...base, newsBeforeOpen: true })
    expect(result.verdict).toBe('wait_news')
    expect(result.score).toBe(8)
    expect(result.summary).toContain('platí až po tisku')
  })

  test('bez dat: samé nulové hlasy s důvodem „bez dat" a bez převahy', () => {
    const result = dayVerdict({ trend: null, positiveGamma: null, tendencyBand: null, sentiment: null, price: null, prevClose: null, oiDelta: null, newsBeforeOpen: false }) // prettier-ignore
    expect(result.score).toBe(0)
    expect(result.verdict).toBe('none')
    expect(result.votes.filter((vote) => vote.reason.includes('bez dat'))).toHaveLength(7)
  })

  test('ΔOI hlasuje jen při rozdílu ≥ 10 % totálu', () => {
    const small = dayVerdict({
      ...base,
      oiDelta: { ...base.oiDelta!, call_delta: 60, put_delta: 20 },
    })
    expect(small.votes.find((vote) => vote.name === 'oi_delta')?.vote).toBe(0)
  })
})
