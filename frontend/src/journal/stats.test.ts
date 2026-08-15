/** Statistiky deníku (#714). */
import { describe, expect, it } from 'vitest'
import type { JournalEntry, JournalTrade } from '../api/journal'
import {
  MIN_SAMPLE,
  closedTrades,
  contextKey,
  detectorComparison,
  groupBy,
  mistakeCost,
  plannedVsRealized,
  rHistogram,
} from './stats'

function trade(patch: Partial<JournalTrade> = {}): JournalTrade {
  return {
    direction: 'long',
    planned_entry: 100,
    planned_stop: 90,
    planned_target: 130,
    actual_entry: 100,
    actual_exit: 110,
    size: null,
    opened_ts: null,
    closed_ts: null,
    setup_key: 'wall_bounce',
    failure_mode: null,
    setup_grade: null,
    execution_grade: null,
    mistake_tags: [],
    emotion: null,
    mfe: null,
    mae: null,
    gross_pnl: null,
    net_pnl: null,
    fees: null,
    ...patch,
  }
}

function entry(patch: Partial<JournalEntry> = {}): JournalEntry {
  return {
    id: 1,
    ts_ref: '2026-08-14T14:30:00+00:00',
    symbol: 'ES',
    entry_type: 'obchod',
    text: 'x',
    tags: [],
    setup_id: null,
    news_event_id: null,
    profile: 'futures',
    trade: trade(),
    context: null,
    daily: null,
    missed_reason: null,
    created_ts: '2026-08-14T14:31:00+00:00',
    updated_ts: null,
    ...patch,
  }
}

describe('closedTrades', () => {
  it('bere jen obchody, ze kterých jde spočítat R', () => {
    const rows = closedTrades([
      entry(),
      entry({ entry_type: 'pozorovani', trade: null }),
      entry({ trade: trade({ actual_exit: null }) }),
    ])
    expect(rows).toHaveLength(1)
    expect(rows[0].r).toBeCloseTo(1) // +10 bodů při riziku 10
  })
})

describe('groupBy', () => {
  it('spočítá expectancy, win rate a profit factor', () => {
    const entries = [
      entry({ trade: trade({ actual_exit: 120 }) }), // +2R
      entry({ trade: trade({ actual_exit: 110 }) }), // +1R
      entry({ trade: trade({ actual_exit: 90 }) }), // −1R
    ]
    const [stats] = groupBy(entries, (_, t) => t.setup_key)
    expect(stats.n).toBe(3)
    expect(stats.wins).toBe(2)
    expect(stats.winRate).toBeCloseTo(2 / 3)
    expect(stats.expectancy).toBeCloseTo((2 + 1 - 1) / 3)
    expect(stats.profitFactor).toBeCloseTo(3)
  })

  it('bez ztrát není profit factor definovaný — nekreslit nekonečno', () => {
    const [stats] = groupBy([entry()], (_, t) => t.setup_key)
    expect(stats.profitFactor).toBeNull()
  })

  it('malý vzorek se označí — pod prahem je závěr náhoda', () => {
    const [small] = groupBy([entry()], (_, t) => t.setup_key)
    expect(small.small).toBe(true)
    const many = Array.from({ length: MIN_SAMPLE }, () => entry())
    expect(groupBy(many, (_, t) => t.setup_key)[0].small).toBe(false)
  })

  it('záznam bez klíče se vynechá — „neměřeno" není kategorie', () => {
    const entries = [entry({ trade: trade({ setup_key: null }) }), entry()]
    const rows = groupBy(entries, (_, t) => t.setup_key)
    expect(rows).toHaveLength(1)
    expect(rows[0].n).toBe(1)
  })
})

describe('mistakeCost', () => {
  it('sečte P/L per tag a řadí od nejdražší', () => {
    const entries = [
      entry({ trade: trade({ mistake_tags: ['late_exit'], net_pnl: -100 }) }),
      entry({ trade: trade({ mistake_tags: ['late_exit', 'fomo'], net_pnl: -50 }) }),
    ]
    const rows = mistakeCost(entries)
    expect(rows[0]).toEqual({ tag: 'late_exit', n: 2, pnl: -150 })
    expect(rows.find((row) => row.tag === 'fomo')?.n).toBe(1)
  })
})

describe('rHistogram', () => {
  it('kbelíkuje po 0,5 R a řadí vzestupně', () => {
    const entries = [
      entry({ trade: trade({ actual_exit: 110 }) }), // +1R
      entry({ trade: trade({ actual_exit: 112 }) }), // +1,2R → koš +1,0
      entry({ trade: trade({ actual_exit: 90 }) }), // −1R
    ]
    const histogram = rHistogram(entries)
    expect(histogram[0].bucket).toBeLessThan(0)
    expect(histogram.find((bucket) => bucket.bucket === 1)?.count).toBe(2)
  })
})

describe('plannedVsRealized', () => {
  it('porovná plánované R:R s realizovaným', () => {
    const result = plannedVsRealized([entry()])
    expect(result?.n).toBe(1)
    expect(result?.avgPlanned).toBeCloseTo(3) // cíl 30 / riziko 10
    expect(result?.avgRealized).toBeCloseTo(1)
  })

  it('bez plánu se nepočítá', () => {
    expect(plannedVsRealized([entry({ trade: trade({ planned_target: null }) })])).toBeNull()
  })
})

describe('detectorComparison', () => {
  it('rozliší vzal / přeskočil / vlastní', () => {
    const result = detectorComparison([
      entry({ setup_id: 5 }),
      entry({ setup_id: null }),
      entry({ entry_type: 'promeskane', trade: null, missed_reason: 'nedovera' }),
      entry({ entry_type: 'pozorovani', trade: null }),
    ])
    expect(result).toEqual({ taken: 1, own: 1, skipped: 1 })
  })
})

describe('contextKey', () => {
  it('vrátí hodnotu, prázdno dá null', () => {
    expect(contextKey(entry({ context: { vol_bucket: 'crisis' } }), 'vol_bucket')).toBe('crisis')
    expect(contextKey(entry({ context: { vol_bucket: null } }), 'vol_bucket')).toBeNull()
    expect(contextKey(entry(), 'vol_bucket')).toBeNull()
  })
})
