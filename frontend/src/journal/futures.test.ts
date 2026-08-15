/** Futures vrstva deníku (#713). */
import { describe, expect, it } from 'vitest'
import type { JournalTrade } from '../api/journal'
import {
  feeShare,
  isRollWeek,
  macroFromHeadline,
  pnlPerContract,
  resultPoints,
  riskPoints,
  sizeGap,
  ticksCaptured,
} from './futures'

function trade(patch: Partial<JournalTrade>): JournalTrade {
  return {
    direction: 'long',
    planned_entry: null,
    planned_stop: null,
    planned_target: null,
    actual_entry: null,
    actual_exit: null,
    size: null,
    opened_ts: null,
    closed_ts: null,
    setup_key: null,
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

describe('R v bodech', () => {
  it('riziko bere skutečný vstup, jinak plánovaný', () => {
    expect(riskPoints(trade({ planned_entry: 6810, planned_stop: 6813 }))).toBe(3)
    expect(riskPoints(trade({ planned_entry: 6810, actual_entry: 6811, planned_stop: 6813 }))).toBe(
      2,
    )
  })

  it('nulové riziko není riziko', () => {
    expect(riskPoints(trade({ planned_entry: 10, planned_stop: 10 }))).toBeNull()
    expect(riskPoints(trade({ planned_entry: 10 }))).toBeNull()
  })

  it('výsledek v bodech respektuje směr', () => {
    const short = trade({ direction: 'short', actual_entry: 6810, actual_exit: 6798 })
    expect(resultPoints(short)).toBe(12)
    expect(resultPoints({ ...short, direction: 'long' })).toBe(-12)
  })

  it('ticky se počítají tickem instrumentu', () => {
    const t = trade({ actual_entry: 6810, actual_exit: 6813 })
    expect(ticksCaptured(t, 'ES')).toBe(12) // 3 body / 0,25
  })

  it('P/L per kontrakt odděluje skill od size', () => {
    const t = trade({ actual_entry: 6810, actual_exit: 6813 })
    // Stejný pohyb: ES 3×50, MES 3×5 — poměr skillu je ale týž
    expect(pnlPerContract(t, 'ES')).toBe(150)
    expect(pnlPerContract(t, 'MES')).toBe(15)
  })

  it('nedokončený obchod nemá výsledek', () => {
    expect(resultPoints(trade({ actual_entry: 6810 }))).toBeNull()
    expect(pnlPerContract(trade({ actual_entry: 6810 }), 'ES')).toBeNull()
  })
})

describe('komise a size', () => {
  it('komisní drag jako podíl hrubého', () => {
    expect(feeShare(trade({ gross_pnl: 200, fees: 40 }))).toBeCloseTo(0.2)
    expect(feeShare(trade({ gross_pnl: 0, fees: 40 }))).toBeNull()
    expect(feeShare(trade({ gross_pnl: 200 }))).toBeNull()
  })

  it('rozdíl plánované a skutečné size je strukturální, ne chyba disciplíny', () => {
    expect(sizeGap(1.4, 1)).toBeCloseTo(-0.4)
    expect(sizeGap(null, 1)).toBeNull()
  })
})

describe('makro tag', () => {
  it('pozná hlavní události', () => {
    expect(macroFromHeadline('US CPI rises 0.3% in July')).toBe('cpi')
    expect(macroFromHeadline('Nonfarm payrolls beat estimates')).toBe('nfp')
    expect(macroFromHeadline('FOMC holds rates steady')).toBe('fomc')
  })

  it('nic nesedí → null, ne „clean" — to jsou různé věci', () => {
    expect(macroFromHeadline('Company X announces buyback')).toBeNull()
    expect(macroFromHeadline('')).toBeNull()
  })
})

describe('roll týden', () => {
  it('týden před kvartální expirací je roll', () => {
    // Zářijová expirace 2026 = 3. pátek = 18. 9. 2026
    expect(isRollWeek(new Date('2026-09-15T12:00:00Z'))).toBe(true)
    expect(isRollWeek(new Date('2026-09-18T12:00:00Z'))).toBe(true)
  })

  it('běžný den roll není', () => {
    expect(isRollWeek(new Date('2026-08-14T12:00:00Z'))).toBe(false)
    expect(isRollWeek(new Date('2026-09-25T12:00:00Z'))).toBe(false)
  })
})
