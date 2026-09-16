/** Kalendář expirací (#1189): popisky chipu, kódy kontraktů, validace odpovědi. */
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  contractCode,
  fetchExpiryCalendar,
  nextContractCode,
  phaseChipLabel,
  phaseTooltip,
  shortDate,
} from './calendar'
import type { ExpiryCalendar } from './calendar'

const BASE: ExpiryCalendar = {
  today: '2026-09-16',
  phase: 'opex_week',
  quarterly_expiry: '2026-09-18',
  roll_date: '2026-09-10',
  soq_ts: '2026-09-18T13:30:00+00:00',
  days_to_expiry: 2,
  is_opex_week: true,
  vix_expiry: '2026-09-16',
  previous_expiry: '2026-06-19',
  markers: [],
}

describe('kalendář expirací', () => {
  afterEach(() => vi.restoreAllMocks())

  it('kódy kontraktů a krátké datum', () => {
    expect(contractCode('2026-09-18')).toBe('U6')
    expect(contractCode('2026-12-18')).toBe('Z6')
    expect(nextContractCode('U6')).toBe('Z6')
    expect(nextContractCode('Z6')).toBe('H7')
    expect(shortDate('2026-09-18')).toBe('18. 9.')
  })

  it('chip podle fáze; běžný den bez chipu', () => {
    expect(phaseChipLabel(BASE)).toMatch(/^OPEX týden · expirace U6 pá 18\. 9\. .*\(SOQ\)$/)
    expect(phaseChipLabel({ ...BASE, phase: 'roll' })).toMatch(/^roll proběhl 10\. 9\./)
    expect(phaseChipLabel({ ...BASE, phase: 'expiry_day' })).toMatch(/^kvartální expirace U6 dnes/)
    expect(phaseChipLabel({ ...BASE, phase: 'post_opex' })).toBe(
      'po OPEXu (18. 9.) — bez opční podpory',
    )
    expect(phaseChipLabel({ ...BASE, phase: 'normal' })).toBeNull()
    const tooltip = phaseTooltip({ ...BASE, phase: 'roll' })
    expect(tooltip).toContain('front kontrakt je další (Z6)')
    expect(tooltip).toContain('SOQ = pátek 9:30 ET')
  })

  it('fetch: validuje tvar, chyba = null', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => ({ ...BASE, markers: undefined }) })),
    )
    const result = await fetchExpiryCalendar('2026-09-16')
    expect(result?.phase).toBe('opex_week')
    expect(result?.markers).toEqual([])
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => ({ phase: 'x' }) })),
    )
    expect(await fetchExpiryCalendar()).toBeNull()
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, json: async () => ({}) })),
    )
    expect(await fetchExpiryCalendar()).toBeNull()
  })
})
