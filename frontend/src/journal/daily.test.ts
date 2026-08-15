/** Denní rituál (#712): segmenty, kontinuita cílů, texty. */
import { describe, expect, it } from 'vitest'
import {
  EMPTY_PLAN,
  cleanScenarios,
  emptyReview,
  isPlanLocked,
  planToText,
  previousGoal,
  reviewToText,
} from './daily'
import { daySegments, segmentForTs } from './segments'

describe('daySegments', () => {
  it('futures dělení pokrývá noc až závěr a segmenty na sebe navazují', () => {
    const segments = daySegments('futures', '2026-08-14')
    expect(segments.map((s) => s.key)).toEqual([
      'globex',
      'premarket',
      'open30',
      'dopoledne',
      'poledne',
      'power',
      'close30',
    ])
    for (let i = 1; i < segments.length; i += 1) {
      expect(segments[i].startMs).toBe(segments[i - 1].endMs)
    }
  })

  it('smb dělení je jiné — u ES by byla půlka polí mrtvá', () => {
    expect(daySegments('smb', '2026-08-14').map((s) => s.key)).toEqual([
      'premarket',
      'open_11',
      '11_12',
      '12_14',
      '14_close',
    ])
  })

  it('hranice jdou ze seance, ne z hardcodu — DST posune UTC čas', () => {
    // Léto: US open 13:30 UTC, zima 14:30 UTC (tatáž lokální 9:30 ET)
    const summer = daySegments('futures', '2026-08-14').find((s) => s.key === 'open30')!
    const winter = daySegments('futures', '2026-12-14').find((s) => s.key === 'open30')!
    expect(new Date(summer.startMs).getUTCHours()).toBe(13)
    expect(new Date(winter.startMs).getUTCHours()).toBe(14)
  })

  it('IB je přesně 30 minut', () => {
    const ib = daySegments('futures', '2026-08-14').find((s) => s.key === 'open30')!
    expect(ib.endMs - ib.startMs).toBe(30 * 60_000)
  })
})

describe('segmentForTs', () => {
  it('zařadí okamžik do správného segmentu', () => {
    expect(segmentForTs('futures', '2026-08-14', '2026-08-14T13:45:00Z')?.key).toBe('open30')
    expect(segmentForTs('futures', '2026-08-14', '2026-08-14T15:00:00Z')?.key).toBe('dopoledne')
  })

  it('noční hodiny patří do Globexu — seance nezačíná ránem', () => {
    expect(segmentForTs('futures', '2026-08-14', '2026-08-14T02:00:00Z')?.key).toBe('globex')
  })

  it('mimo pokryté hodiny nevymýšlí', () => {
    // Po US close (20:00 UTC v létě) už žádný segment neběží
    expect(segmentForTs('futures', '2026-08-14', '2026-08-14T21:00:00Z')).toBeNull()
    expect(segmentForTs('futures', '2026-08-14', 'nesmysl')).toBeNull()
  })
})

describe('plán a vyhodnocení', () => {
  it('zamčení je vidět a prázdný plán zamčený není', () => {
    expect(isPlanLocked(EMPTY_PLAN)).toBe(false)
    expect(isPlanLocked({ ...EMPTY_PLAN, locked_ts: '2026-08-14T12:00:00Z' })).toBe(true)
  })

  it('prázdné scénáře se nezapisují — prázdný řádek není plán', () => {
    const scenarios = [
      { condition: '', action: '' },
      { condition: 'nad flipem', action: 'long' },
      { condition: '  ', action: '  ' },
    ]
    expect(cleanScenarios(scenarios)).toHaveLength(1)
  })

  it('cíl na zítřek se bere z posledního vyhodnocení PŘED dneškem', () => {
    const entries = [
      {
        ts_ref: '2026-08-12T20:00:00Z',
        daily: {
          review: { segments: [], lesson: '', tomorrow_goal: 'starý', plan_entry_id: null },
        },
      },
      {
        ts_ref: '2026-08-13T20:00:00Z',
        daily: { review: { segments: [], lesson: '', tomorrow_goal: 'max 3 obchody', plan_entry_id: null } }, // prettier-ignore
      },
      // Dnešní vyhodnocení se do dnešního plánu propsat nesmí
      {
        ts_ref: '2026-08-14T20:00:00Z',
        daily: {
          review: { segments: [], lesson: '', tomorrow_goal: 'dnešní', plan_entry_id: null },
        },
      },
    ]
    expect(previousGoal(entries, '2026-08-14')).toBe('max 3 obchody')
    expect(previousGoal([], '2026-08-14')).toBe('')
  })

  it('planToText nese scénáře i procesní cíl', () => {
    const text = planToText({
      ...EMPTY_PLAN,
      prev_goal: 'držet stop',
      scenarios: [{ condition: 'nad flipem', action: 'long k call wall' }],
      process_goal: 'max 3 obchody',
      mental_state: 4,
    })
    expect(text).toContain('Včerejší cíl: držet stop')
    expect(text).toContain('Když nad flipem → long k call wall')
    expect(text).toContain('Procesní cíl: max 3 obchody')
    expect(text).toContain('Stav: 4/5')
  })

  it('reviewToText vynechá nevyplněné segmenty', () => {
    const review = emptyReview(['open30', 'poledne'])
    review.segments[0] = { key: 'open30', grade: 'B', note: 'nečekal jsem' }
    review.lesson = 'čekat na potvrzení'
    const text = reviewToText(review, { open30: 'US open +30', poledne: 'Poledne' })
    expect(text).toContain('US open +30: B — nečekal jsem')
    expect(text).not.toContain('Poledne')
    expect(text).toContain('Lekce: čekat na potvrzení')
  })
})
