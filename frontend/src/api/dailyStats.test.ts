/** Denní statistika setupů (#748).

Testy míří na past, kvůli které tahle funkce má vlastní parametr `toSessionDate`:
den je **obchodní seance**, ne kalendářní datum. Noční Globex obchod po 17:00 CT
už patří následujícímu dni a naivní `toISOString().slice(0, 10)` by ho zařadil
špatně — a nikdo by si toho nevšiml, protože číslo by pořád vypadalo rozumně.
*/
import { describe, expect, it } from 'vitest'
import { dailyStats } from './setups'
import type { SetupRow } from './setups'
import { sessionDateIso } from '../instrument/tz'

const POINT_USD = 50 // ES

function row(overrides: Partial<SetupRow>): SetupRow {
  return {
    id: 1,
    symbol: 'ES',
    expiry: '20260817',
    template: 'wall_bounce',
    direction: 'long',
    created_ts: '2026-08-17T14:00:00Z',
    entry: 6000,
    target: 6010,
    stop: 5995, // riziko 5 bodů × 50 $ = 250 $ = 5 % účtu
    confidence: 0.6,
    reason: '',
    status: 'closed_target',
    closed_ts: '2026-08-17T15:00:00Z',
    outcome_r: 2,
    mfe: null,
    mae: null,
    rating: null,
    user_note: null,
    ...overrides,
  } as SetupRow
}

const TODAY = '2026-08-17'
const bySession = (ts: number) => sessionDateIso(ts)

describe('dailyStats', () => {
  it('spočítá bilanci dne', () => {
    const stats = dailyStats(
      [
        row({ id: 1, outcome_r: 2 }), // +500 $
        row({ id: 2, outcome_r: -1 }), // −250 $
        row({ id: 3, outcome_r: 3 }), // +750 $
      ],
      POINT_USD,
      TODAY,
      bySession,
    )

    expect(stats.trades).toBe(3)
    expect(stats.wins).toBe(2)
    expect(stats.losses).toBe(1)
    expect(stats.winRate).toBeCloseTo(66.67, 1)
    expect(stats.bestUsd).toBe(750)
    expect(stats.worstUsd).toBe(-250)
    expect(stats.pnlUsd).toBe(1000)
    expect(stats.pnlPct).toBeCloseTo(20, 5) // 1000 / 5000
  })

  it('počítá riziko i z aktivních pozic', () => {
    const stats = dailyStats(
      [
        row({ id: 1, status: 'active', closed_ts: null, outcome_r: null }),
        row({ id: 2, outcome_r: 1 }),
      ],
      POINT_USD,
      TODAY,
      bySession,
    )

    expect(stats.active).toBe(1)
    expect(stats.closed).toBe(1)
    // „Kolik bylo v sázce" je otázka o vstupu, ne o výsledku
    expect(stats.maxRiskPct).toBeCloseTo(5, 5)
    expect(stats.totalRiskPct).toBeCloseTo(10, 5)
  })

  it('prázdný den nevydává za ztrátový', () => {
    const stats = dailyStats([row({ created_ts: '2026-08-10T14:00:00Z', closed_ts: '2026-08-10T15:00:00Z' })], POINT_USD, TODAY, bySession) // prettier-ignore

    expect(stats.trades).toBe(0)
    expect(stats.winRate).toBeNull() // ne 0 %
    expect(stats.bestUsd).toBeNull()
    expect(stats.pnlUsd).toBe(0)
  })

  it('den bez uzavřeného obchodu nemá úspěšnost', () => {
    const stats = dailyStats(
      [row({ status: 'active', closed_ts: null, outcome_r: null })],
      POINT_USD,
      TODAY,
      bySession,
    )

    expect(stats.trades).toBe(1)
    expect(stats.closed).toBe(0)
    expect(stats.winRate).toBeNull()
  })

  it('noční Globex obchod patří NÁSLEDUJÍCÍ seanci, ne kalendářnímu dni', () => {
    // 2026-08-17 23:00 UTC = 18:00 CT → seance už je 18. 8.
    const nocni = row({
      created_ts: '2026-08-17T23:00:00Z',
      closed_ts: '2026-08-17T23:30:00Z',
      outcome_r: 1,
    })

    expect(dailyStats([nocni], POINT_USD, '2026-08-17', bySession).trades).toBe(0)
    expect(dailyStats([nocni], POINT_USD, '2026-08-18', bySession).trades).toBe(1)
  })

  it('uzavřený setup patří do dne uzavření, ne vzniku', () => {
    // Vznikl v úterý odpoledne, uzavřel se ve středu ráno
    const prestupujici = row({
      created_ts: '2026-08-17T18:00:00Z', // 13:00 CT → seance 17. 8.
      closed_ts: '2026-08-18T13:00:00Z', // 08:00 CT → seance 18. 8.
      outcome_r: 1,
    })

    expect(dailyStats([prestupujici], POINT_USD, '2026-08-17', bySession).trades).toBe(0)
    expect(dailyStats([prestupujici], POINT_USD, '2026-08-18', bySession).trades).toBe(1)
  })

  it('aktivní setup se řadí podle vzniku (jiný čas nemá)', () => {
    const aktivni = row({
      created_ts: '2026-08-17T18:00:00Z',
      closed_ts: null,
      status: 'active',
      outcome_r: null,
    })

    expect(dailyStats([aktivni], POINT_USD, '2026-08-17', bySession).active).toBe(1)
  })

  it('timeout se počítá jako uzavřený obchod', () => {
    const stats = dailyStats(
      [row({ status: 'closed_timeout', outcome_r: -0.5 })],
      POINT_USD,
      TODAY,
      bySession,
    )

    expect(stats.closed).toBe(1)
    expect(stats.losses).toBe(1)
    expect(stats.pnlUsd).toBe(-125)
  })
})
