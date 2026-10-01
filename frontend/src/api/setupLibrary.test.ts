/** Knihovna setupů (#1323): popisky buněk, parametry, chyby API, zápis stádia. */
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  apiErrorText,
  decisionText,
  decisionTooltip,
  edgeUnproven,
  evidenceTooltip,
  gateLabel,
  lbText,
  libraryParams,
  proofText,
  saveSetupStage,
  stageLabel,
  trialEnd,
  trialUsageText,
  windowTooShort,
} from './setupLibrary'
import type { LibraryCell } from './setupLibrary'
import { riskInfo, riskLabel, riskTooltip } from './setups'

const CELL: LibraryCell = {
  cell: 'NQ:trend_continuation',
  ticker: 'NQ',
  template: 'trend_continuation',
  template_number: 7,
  stage: 'auto',
  effective_stage: 'auto',
  trial: null,
  gate_verdict: 'block',
  gate_n: 163,
  gate_lb: -0.14,
  avg_r: 0.09,
  avg_net_r: 0.03,
  net_usd: -120.5,
  n_needed: 462,
  sessions: 25,
  per_session: 6.52,
  window_capacity: 489,
  sessions_to_decision: 46,
}

const TRIAL = {
  started_at: '2026-10-01T14:05:00+00:00',
  budget_setups: 10,
  budget_r: 3,
  mechanics_version: 5,
  setups: 3,
  sum_r: -1,
  spent: false,
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('popisky buňky', () => {
  it('brána teď: verdikt, nedostatek vzorku proti minimu a dolní mez', () => {
    expect(gateLabel(CELL, 30)).toBe('✕ block')
    expect(gateLabel({ gate_verdict: 'insufficient', gate_n: 24 }, 30)).toBe('· 24/30')
    expect(gateLabel({ gate_verdict: 'pass', gate_n: 40 }, 30)).toBe('✓ pass')
    expect(lbText(-0.14)).toBe('LB -0.14')
    expect(lbText(null)).toBe('LB —')
  })

  it('průkaznost a odhad v seancích říkají poctivě, kdy vzorek nestačí', () => {
    expect(proofText(CELL)).toBe('163 / 462')
    expect(proofText({ gate_n: 1, n_needed: null })).toBe('1 / —')
    expect(decisionText(CELL)).toBe('~46 seancí')
    const odhad = { gate_n: 10, window_capacity: 600 }
    expect(decisionText({ ...odhad, sessions_to_decision: 3, n_needed: 50 })).toBe('~3 seance')
    expect(decisionText({ ...odhad, sessions_to_decision: 0, n_needed: 50 })).toBe('vzorek stačí')
    expect(decisionText({ ...odhad, sessions_to_decision: null, n_needed: null })).toBe('málo dat')
  })

  it('okno brány, které vzorek nepojme, se nehlásí jako blížící se rozhodnutí', () => {
    // NQ T7 1. 10.: 163 vzorků za 29 seancí ≈ 5,6/seanci → okno 60 seancí pojme ~337 < 462
    const nq = { ...CELL, window_capacity: 337, sessions_to_decision: null }
    expect(windowTooShort(nq)).toBe(true)
    expect(decisionText(nq)).toBe('v okně nedosáhne · max ~337/462')
    const tooltip = decisionTooltip(nq, libraryParams(null))
    expect(tooltip).toContain('• okno brány při tomto tempu pojme: ~337 vzorků (tempo × 60 seancí)')
    expect(tooltip).toContain('• 337 < 462: v okně 60 seancí vzorek nenaroste')
    expect(windowTooShort(CELL)).toBe(false)
    expect(decisionTooltip(CELL, libraryParams(null))).not.toContain('nenaroste')
  })

  it('ØR tooltip ukazuje i čistý P/L v mikro dolarech', () => {
    expect(evidenceTooltip(CELL)).toContain('• čistě v $: -120.5 $ — Σ za vzorek v reálných mikro')
    expect(evidenceTooltip({ ...CELL, net_usd: null })).toContain('• čistě v $: —')
  })

  it('edge neprokázán při LB ≤ 0 i bez dolní meze', () => {
    expect(edgeUnproven(CELL)).toBe(true)
    expect(edgeUnproven({ gate_lb: null })).toBe(true)
    expect(edgeUnproven({ gate_lb: 0 })).toBe(true)
    expect(edgeUnproven({ gate_lb: 0.01 })).toBe(false)
  })

  it('skončená zkouška se hlásí jako Auto i s důvodem, čerpání se znaménkem', () => {
    const spent = { ...TRIAL, setups: 10, spent: true }
    expect(stageLabel({ stage: 'trial', effective_stage: 'trial', trial: TRIAL })).toBe('Zkouška')
    expect(stageLabel({ stage: 'trial', effective_stage: 'auto', trial: spent })).toBe(
      'Auto (zkouška vyčerpána)',
    )
    expect(trialEnd({ effective_stage: 'auto', trial: spent })).toBe('budget')
    // Nevyčerpaná, a přesto Auto = zkouška začala na jiné mechanice
    expect(stageLabel({ stage: 'trial', effective_stage: 'auto', trial: TRIAL })).toBe(
      'Auto (zkouška skončila)',
    )
    expect(trialEnd({ effective_stage: 'auto', trial: TRIAL })).toBe('mechanics')
    expect(trialEnd({ effective_stage: 'trial', trial: TRIAL })).toBeNull()
    expect(stageLabel({ stage: 'shadow', effective_stage: 'shadow', trial: null })).toBe('Stín')
    expect(trialUsageText(TRIAL)).toBe('3/10 · -1.0 z -3.0 R')
  })
})

describe('parametry Knihovny', () => {
  it('platná verze přebije defaulty, chybějící klíč bere default serveru', () => {
    const params = libraryParams({
      current: {
        version: 4,
        created_ts: '2026-10-01T12:00:00+00:00',
        created_by: 'ui',
        note: 'x',
        params: { template_gate_days: 40, disabled_templates: ['divergence_spring'] },
      },
      defaults: { template_gate_days: 60, template_gate_min_samples: 30, trial_budget_setups: 10 },
    })
    expect(params.version).toBe(4)
    expect(params.gateDays).toBe(40)
    expect(params.gateMinSamples).toBe(30)
    expect(params.trialBudgetSetups).toBe(10)
    expect(params.trialBudgetR).toBe(3) // ani verze, ani defaulty → konstanta
    expect(params.disabledTemplates).toEqual(['divergence_spring'])
  })

  it('bez odpovědi serveru drží rozumné výchozí hodnoty', () => {
    const params = libraryParams(null)
    expect(params.version).toBeNull()
    expect([params.gateDays, params.gateMinSamples]).toEqual([60, 30])
  })
})

describe('chyby API', () => {
  it('detail jako text i jako pole chyb pydantic', () => {
    expect(apiErrorText('Neznámá šablona', 422)).toBe('Neznámá šablona')
    expect(apiErrorText([{ msg: 'String should have at least 3 characters' }], 422)).toBe(
      'String should have at least 3 characters',
    )
    expect(apiErrorText(undefined, 503)).toBe('HTTP 503')
  })
})

describe('zápis stádia', () => {
  it('rozpočet posílá jen u zkoušky, začátek nevolí klient', async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ version: 5 }) }))
    vi.stubGlobal('fetch', fetchMock)
    const shadow = await saveSetupStage({
      cell: 'ES:trend_continuation',
      stage: 'shadow',
      note: 'edge neprokázán',
      budgetSetups: 10,
      budgetR: 3,
    })
    expect(shadow).toEqual({ ok: true, version: 5 })
    const trial = await saveSetupStage({
      cell: 'NQ:trend_continuation',
      stage: 'trial',
      note: 'test naživo',
      budgetSetups: 8,
      budgetR: 2.5,
    })
    expect(trial.ok).toBe(true)
    const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>
    const bodies = calls.map(([url, init]) => {
      expect(url).toContain('/setups/stage')
      expect(init.method).toBe('POST')
      return JSON.parse(String(init.body)) as Record<string, unknown>
    })
    expect(bodies[0]).toEqual({
      cell: 'ES:trend_continuation',
      stage: 'shadow',
      note: 'edge neprokázán',
      created_by: 'ui',
    })
    expect(bodies[1]).toMatchObject({ stage: 'trial', budget_setups: 8, budget_r: 2.5 })
    expect(bodies[1]).not.toHaveProperty('started_at')
  })

  it('chyba serveru je text, ne výjimka', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 422,
        json: async () => ({ detail: 'Zkouška: rozpočet 25 setupů je mimo meze 1–20' }),
      })),
    )
    const result = await saveSetupStage({ cell: 'NQ:x', stage: 'trial', note: 'abc' })
    expect(result).toEqual({ ok: false, error: 'Zkouška: rozpočet 25 setupů je mimo meze 1–20' })
  })
})

describe('kontext setupu se stádiem (risk_rules_version 3)', () => {
  const base = {
    risk_rules_version: 3,
    account_equity_usd: 50000,
    risk_budget_usd: 500,
    stop_points: 15,
    contracts: 1,
    max_loss_usd: 300,
    fee_usd: 10,
    affordable: true,
    template_gate: 'block',
    template_gate_n: 163,
    template_gate_lb: -0.14,
    realized_day_r: 0,
    realized_week_r: 0,
  }

  it('zkouška: obchodovatelný s přebitou bránou, štítek s čerpáním', () => {
    const info = riskInfo({
      context: {
        ...base,
        tradeable: true,
        trade_block: null,
        user_stage: 'trial',
        gate_overridden: true,
        trial_setups: 3,
        trial_budget_setups: 10,
        trial_sum_r: -1,
        trial_budget_r: 3,
      },
    })
    expect(info).not.toBeNull()
    expect(riskLabel(info!)).toBe('1 ks · 300 $ · zkouška 3/10')
    expect(riskTooltip(info!)).toContain('• stádium v Knihovně: Zkouška — přebila bránu (3/10')
  })

  it('stín uživatele má vlastní důvod; starší řádek stádium nenese', () => {
    const shadow = riskInfo({
      context: { ...base, tradeable: false, trade_block: 'user', user_stage: 'shadow' },
    })
    expect(riskLabel(shadow!)).toBe('stín: ve stínu z rozhodnutí uživatele')
    expect(riskTooltip(shadow!)).toContain('Stín — rozhodnutí uživatele')
    const legacy = riskInfo({ context: { ...base, tradeable: false, trade_block: 'gate' } })
    expect(legacy!.userStage).toBeNull()
    expect(riskTooltip(legacy!)).not.toContain('stádium v Knihovně')
  })
})
