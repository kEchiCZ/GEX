/** Risk framework malého účtu (#1185): čtení kontextu, štítky, bilance účtu. */
import { describe, expect, it } from 'vitest'
import {
  ACCOUNT_START_USD,
  accountPnlUsd,
  accountStats,
  riskInfo,
  riskLabel,
  riskTooltip,
} from './setups'
import type { SetupRow } from './setups'

const RISK = {
  risk_rules_version: 1,
  account_equity_usd: 50000,
  point_value_usd: 50,
  risk_budget_usd: 500,
  stop_points: 8,
  contracts: 1,
  max_loss_usd: 400,
  fee_usd: 10,
  affordable: true,
  tradeable: true,
  trade_block: null,
  template_gate: 'pass',
  template_gate_n: 35,
  template_gate_lb: 0.12,
  realized_day_r: -1,
  realized_week_r: 2.5,
}

function row(context: Record<string, unknown>, outcome_r: number | null, closed: string): SetupRow {
  return {
    id: 1,
    symbol: 'ES',
    expiry: '20260916',
    template: 'failed_break',
    direction: 'long',
    created_ts: '2026-09-16T14:00:00+00:00',
    entry: 7600,
    target: 7620,
    stop: 7592,
    confidence: 50,
    reason: 'x',
    status: outcome_r === null ? 'active' : outcome_r > 0 ? 'closed_target' : 'closed_stop',
    closed_ts: outcome_r === null ? null : closed,
    outcome_r,
    mfe: null,
    mae: null,
    user_rating: null,
    user_note: null,
    mechanics_version: 5,
    context,
  }
}

describe('riskInfo', () => {
  it('účet v jednotkách aplikace je 50 000 $ (MES/MNQ × 10)', () => {
    expect(ACCOUNT_START_USD).toBe(50000)
  })

  it('čte kontext obchodovatelného setupu a skládá štítek', () => {
    const info = riskInfo({ context: RISK })
    expect(info).not.toBeNull()
    expect(info!.tradeable).toBe(true)
    expect(info!.contracts).toBe(1)
    expect(riskLabel(info!)).toBe('1 ks · 400 $')
    const tooltip = riskTooltip(info!)
    expect(tooltip).toContain('Obchodovatelný: 1 kontrakt(y), ztráta na stopu 400 $')
    expect(tooltip).toContain('prošla (dolní mez očekávání > 0) · n=35 · LB +0.12 R')
    expect(tooltip).toContain('dnes -1.0 R, týden +2.5 R')
  })

  it('stín nese důvod; řádek bez pravidel je null', () => {
    const shadow = riskInfo({
      context: { ...RISK, tradeable: false, contracts: 0, trade_block: 'daily_brake' },
    })
    expect(riskLabel(shadow!)).toBe('stín: denní brzda')
    expect(riskTooltip(shadow!)).toContain('Stínový setup — neobchodovat: denní brzda.')
    expect(riskInfo({ context: { gex_regime: 'negative' } })).toBeNull()
    expect(riskInfo({ context: null })).toBeNull()
    // Neznámý důvod se nevymýšlí
    const odd = riskInfo({ context: { ...RISK, tradeable: false, trade_block: 'whatever' } })
    expect(odd!.block).toBeNull()
    expect(riskLabel(odd!)).toBe('stín: neobchodovatelný')
  })
})

describe('bilance účtu', () => {
  it('P/L = R × ztráta na stopu − poplatky; stín a aktivní bez P/L', () => {
    expect(accountPnlUsd(row(RISK, 2, '2026-09-16T15:00:00+00:00'))).toBe(790)
    expect(accountPnlUsd(row(RISK, -1, '2026-09-16T15:00:00+00:00'))).toBe(-410)
    expect(
      accountPnlUsd(row({ ...RISK, tradeable: false }, 2, '2026-09-16T15:00:00+00:00')),
    ).toBeNull()
    expect(accountPnlUsd(row(RISK, null, ''))).toBeNull()
  })

  it('accountStats: chronologicky, max DD, stín se počítá zvlášť', () => {
    const rows = [
      row(RISK, 2, '2026-09-16T17:00:00+00:00'), // +790 (třetí)
      row(RISK, -1, '2026-09-16T15:00:00+00:00'), // −410 (první)
      row(RISK, -1, '2026-09-16T16:00:00+00:00'), // −410 (druhá) → DD −820
      row({ ...RISK, tradeable: false, trade_block: 'gate' }, -3, '2026-09-16T16:30:00+00:00'),
      row({ gex_regime: 'negative' }, -5, '2026-09-16T16:40:00+00:00'), // před pravidly
    ]
    const stats = accountStats(rows)
    expect(stats).toEqual({ n: 3, shadow: 1, pnlUsd: -30, feesUsd: 30, maxDrawdownUsd: -820 })
    expect(
      accountStats([row({ gex_regime: 'negative' }, 1, '2026-09-16T16:40:00+00:00')]),
    ).toBeNull()
  })
})
