/** Převody rozepsaného obchodu (#709). */
import { describe, expect, it } from 'vitest'
import type { JournalTrade } from '../api/journal'
import { plannedRR, realizedR } from '../api/journal'
import { EMPTY_TRADE, draftToTrade, tradeToDraft } from './trade'

describe('draftToTrade', () => {
  it('prázdné pole dá null, ne nulu — nula je platná cena', () => {
    const trade = draftToTrade(EMPTY_TRADE)
    expect(trade.planned_entry).toBeNull()
    expect(trade.net_pnl).toBeNull()
    expect(trade.direction).toBe('long')
  })

  it('desetinná čárka i tečka projdou stejně', () => {
    expect(draftToTrade({ ...EMPTY_TRADE, plannedEntry: '6810,25' }).planned_entry).toBe(6810.25)
    expect(draftToTrade({ ...EMPTY_TRADE, plannedEntry: '6810.25' }).planned_entry).toBe(6810.25)
  })

  it('rozepsaný nesmysl skončí jako null, ne NaN', () => {
    expect(draftToTrade({ ...EMPTY_TRADE, plannedStop: '-' }).planned_stop).toBeNull()
    expect(draftToTrade({ ...EMPTY_TRADE, plannedStop: 'abc' }).planned_stop).toBeNull()
  })

  it('kolo tam a zpět zachová hodnoty', () => {
    const draft = {
      ...EMPTY_TRADE,
      direction: 'short' as const,
      plannedEntry: '6810',
      plannedStop: '6813',
      setupGrade: 'A' as const,
      mistakeTags: ['late_exit'],
    }
    const round = tradeToDraft(draftToTrade(draft) as JournalTrade)
    expect(round.direction).toBe('short')
    expect(round.plannedEntry).toBe('6810')
    expect(round.setupGrade).toBe('A')
    expect(round.mistakeTags).toEqual(['late_exit'])
  })
})

describe('odvozené hodnoty', () => {
  const base = draftToTrade(EMPTY_TRADE) as JournalTrade

  it('plannedRR = cíl ku riziku', () => {
    const trade = { ...base, planned_entry: 6810, planned_stop: 6813, planned_target: 6798 }
    expect(plannedRR(trade)).toBeCloseTo(4)
  })

  it('nulové riziko nedělí nulou', () => {
    expect(
      plannedRR({ ...base, planned_entry: 10, planned_stop: 10, planned_target: 12 }),
    ).toBeNull()
  })

  it('realizedR respektuje směr — short vydělá poklesem', () => {
    const short = {
      ...base,
      direction: 'short' as const,
      planned_stop: 6813,
      actual_entry: 6810,
      actual_exit: 6798,
    }
    expect(realizedR(short)).toBeCloseTo(4)
    const long = { ...short, direction: 'long' as const }
    expect(realizedR(long)).toBeCloseTo(-4)
  })

  it('neuzavřený obchod nemá R', () => {
    expect(realizedR({ ...base, actual_entry: 6810, planned_stop: 6813 })).toBeNull()
  })
})
