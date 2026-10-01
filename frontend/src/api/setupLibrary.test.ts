/** Knihovna setupů (#1323): popisky buněk, parametry, chyby API, zápis stádia. */
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  apiErrorText,
  clearLibraryFilters,
  decisionText,
  decisionTooltip,
  DEFAULT_LIBRARY_VIEW,
  edgeUnproven,
  evidenceTooltip,
  filterLibraryCells,
  gateLabel,
  lbText,
  libraryFiltersActive,
  libraryParams,
  libraryRows,
  libraryGateFilterOf,
  librarySortKeyOf,
  librarySortTooltip,
  libraryTickers,
  nextLibrarySort,
  proofText,
  reviveLibraryView,
  saveSetupStage,
  sortLibraryCells,
  stageLabel,
  toggleLibraryStage,
  trialEnd,
  trialUsageText,
  windowTooShort,
} from './setupLibrary'
import type { LibraryCell, LibraryView } from './setupLibrary'
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

describe('řazení a filtry tabulky', () => {
  // Pořadí serveru: průkaznost sestupně (163/462, 30/100, 12/618, bez odhadu)
  const A: LibraryCell = { ...CELL, cell: 'NQ:trend_continuation' }
  const B: LibraryCell = {
    ...CELL,
    cell: 'ES:gamma_momentum',
    ticker: 'ES',
    template: 'gamma_momentum',
    template_number: 4,
    stage: 'shadow',
    effective_stage: 'shadow',
    gate_verdict: 'insufficient',
    gate_n: 30,
    gate_lb: null,
    avg_r: null,
    avg_net_r: 0.2,
    n_needed: 100,
    sessions_to_decision: 3,
  }
  const C: LibraryCell = {
    ...CELL,
    cell: 'ES:trend_continuation',
    ticker: 'ES',
    gate_verdict: 'pass',
    gate_n: 12,
    gate_lb: 0.3,
    avg_r: 0.5,
    avg_net_r: null,
    n_needed: 618,
    sessions_to_decision: null,
  }
  const D: LibraryCell = {
    ...CELL,
    cell: 'NQ:divergence_spring',
    template: 'divergence_spring',
    template_number: 9,
    stage: 'trial',
    effective_stage: 'auto',
    trial: { ...TRIAL, spent: true },
    gate_verdict: 'block',
    gate_lb: -0.5,
    avg_r: -0.2,
    avg_net_r: -0.3,
    n_needed: null,
    sessions_to_decision: null,
  }
  const SERVER = [A, B, C, D]
  const ids = (cells: LibraryCell[]) => cells.map((cell) => cell.cell)
  const view = (patch: Partial<LibraryView>): LibraryView => ({ ...DEFAULT_LIBRARY_VIEW, ...patch })

  it('výchozí pohled = průkaznost sestupně = pořadí serveru, vstup se nemění', () => {
    const input = [...SERVER]
    expect(ids(libraryRows(input, DEFAULT_LIBRARY_VIEW))).toEqual(ids(SERVER))
    expect(ids(libraryRows([D, C, B, A], DEFAULT_LIBRARY_VIEW))).toEqual(ids(SERVER))
    sortLibraryCells(input, 'ticker', 'asc')
    expect(input).toEqual(SERVER)
  })

  it('číselné sloupce číselně, prázdné hodnoty na konec v obou směrech', () => {
    expect(ids(sortLibraryCells(SERVER, 'avgR', 'asc'))).toEqual([D.cell, A.cell, C.cell, B.cell])
    expect(ids(sortLibraryCells(SERVER, 'avgR', 'desc'))).toEqual([C.cell, A.cell, D.cell, B.cell])
    expect(ids(sortLibraryCells(SERVER, 'avgNetR', 'asc'))).toEqual([
      D.cell,
      A.cell,
      B.cell,
      C.cell,
    ])
    expect(ids(sortLibraryCells(SERVER, 'avgNetR', 'desc'))).toEqual([
      B.cell,
      A.cell,
      D.cell,
      C.cell,
    ])
    expect(ids(sortLibraryCells(SERVER, 'proof', 'asc'))).toEqual([C.cell, B.cell, A.cell, D.cell])
    // Rozhodnutelné: C „v okně nedosáhne" (okno 489 < 618) je nejdál — vzestupně
    // za odhady, sestupně první; D „málo dat" (bez n potřebné) dole v obou směrech
    expect(ids(sortLibraryCells(SERVER, 'decision', 'asc'))).toEqual([
      B.cell,
      A.cell,
      C.cell,
      D.cell,
    ])
    expect(ids(sortLibraryCells(SERVER, 'decision', 'desc'))).toEqual([
      C.cell,
      A.cell,
      B.cell,
      D.cell,
    ])
    // Dvě „v okně nedosáhne" jsou shoda (∞ − ∞ ne NaN) — drží vstupní pořadí
    const far = { ...C, cell: 'NQ:far' }
    expect(ids(sortLibraryCells([far, B, C], 'decision', 'desc'))).toEqual([
      'NQ:far',
      C.cell,
      B.cell,
    ])
    // „vzorek stačí" (0) je nejblíž rozhodnutí
    const ready = { ...A, cell: 'NQ:ready', sessions_to_decision: 0 }
    expect(ids(sortLibraryCells([A, B, ready], 'decision', 'asc'))[0]).toBe('NQ:ready')
    // Číslo šablony číselně: T10 za T9, ne mezi T1 a T2
    const t10 = { ...A, cell: 'NQ:t10', template_number: 10 }
    expect(ids(sortLibraryCells([t10, D, B], 'setup', 'asc'))).toEqual([B.cell, D.cell, 'NQ:t10'])
  })

  it('ticker podle kódových jednotek jako výběr tickeru, ne české CH za H', () => {
    const hd = { ...A, cell: 'HD:trend_continuation', ticker: 'HD' }
    const chtr = { ...A, cell: 'CHTR:trend_continuation', ticker: 'CHTR' }
    expect(ids(sortLibraryCells([hd, chtr], 'ticker', 'asc'))).toEqual([chtr.cell, hd.cell])
    expect(libraryTickers([hd, chtr], null)).toEqual(['CHTR', 'HD'])
  })

  it('shoda drží vstupní pořadí (stabilní) v obou směrech', () => {
    expect(ids(sortLibraryCells(SERVER, 'ticker', 'asc'))).toEqual([B.cell, C.cell, A.cell, D.cell])
    expect(ids(sortLibraryCells(SERVER, 'ticker', 'desc'))).toEqual([
      A.cell,
      D.cell,
      B.cell,
      C.cell,
    ])
  })

  it('stádium podle platného stádia, brána podle verdiktu a uvnitř podle LB', () => {
    // Vyčerpaná zkouška (D) se řadí jako Auto
    expect(ids(sortLibraryCells(SERVER, 'stage', 'asc'))).toEqual([A.cell, C.cell, D.cell, B.cell])
    // block (D −0.5, A −0.14) → insufficient (B) → pass (C)
    expect(ids(sortLibraryCells(SERVER, 'gate', 'asc'))).toEqual([D.cell, A.cell, B.cell, C.cell])
    expect(ids(sortLibraryCells(SERVER, 'gate', 'desc'))).toEqual([C.cell, B.cell, A.cell, D.cell])
    // LB bez hodnoty je uvnitř verdiktu poslední
    const noLb = { ...A, cell: 'NQ:no_lb', gate_lb: null }
    expect(ids(sortLibraryCells([noLb, A, D], 'gate', 'desc'))).toEqual([
      A.cell,
      D.cell,
      'NQ:no_lb',
    ])
    // Brána vypnutá (off) nemá pořadí → na konec
    const off = { ...C, cell: 'ES:off', gate_verdict: 'off' as const }
    expect(ids(sortLibraryCells([off, D], 'gate', 'desc'))).toEqual([D.cell, 'ES:off'])
  })

  it('filtry: ticker, stádia, brána, hledání bez diakritiky a ØR čistě > 0', () => {
    expect(ids(filterLibraryCells(SERVER, view({ ticker: 'ES' })))).toEqual([B.cell, C.cell])
    expect(ids(filterLibraryCells(SERVER, view({ stages: ['auto'] })))).toEqual([
      A.cell,
      C.cell,
      D.cell,
    ])
    expect(ids(filterLibraryCells(SERVER, view({ stages: ['shadow', 'trial'] })))).toEqual([B.cell])
    expect(ids(filterLibraryCells(SERVER, view({ gate: 'block' })))).toEqual([A.cell, D.cell])
    expect(ids(filterLibraryCells(SERVER, view({ query: '  POKRACOVANI ' })))).toEqual([
      A.cell,
      C.cell,
    ])
    expect(ids(filterLibraryCells(SERVER, view({ query: 't9' })))).toEqual([D.cell])
    // Bez ØR čistě (C) a záporné (D) skryje
    expect(ids(filterLibraryCells(SERVER, view({ netPositive: true })))).toEqual([A.cell, B.cell])
    expect(
      ids(libraryRows(SERVER, view({ ticker: 'NQ', sortKey: 'setup', sortDir: 'desc' }))),
    ).toEqual([D.cell, A.cell])
  })

  it('klik na záhlaví, přepínání stádií a zrušení filtrů', () => {
    const byTicker = nextLibrarySort(DEFAULT_LIBRARY_VIEW, 'ticker')
    expect([byTicker.sortKey, byTicker.sortDir]).toEqual(['ticker', 'asc'])
    expect(nextLibrarySort(byTicker, 'ticker').sortDir).toBe('desc')
    expect(nextLibrarySort(nextLibrarySort(byTicker, 'ticker'), 'ticker').sortDir).toBe('asc')
    // Výchozí Průkaznost ↓ → klik na ni = vzestupně
    expect(nextLibrarySort(DEFAULT_LIBRARY_VIEW, 'proof').sortDir).toBe('asc')
    const staged = toggleLibraryStage(toggleLibraryStage(DEFAULT_LIBRARY_VIEW, 'trial'), 'auto')
    expect(staged.stages).toEqual(['auto', 'trial'])
    expect(toggleLibraryStage(staged, 'auto').stages).toEqual(['trial'])
    const filtered = view({ sortKey: 'gate', ticker: 'NQ', stages: ['auto'], query: 'x' })
    expect(libraryFiltersActive(DEFAULT_LIBRARY_VIEW)).toBe(false)
    expect(libraryFiltersActive(view({ query: '   ' }))).toBe(false)
    expect(libraryFiltersActive(filtered)).toBe(true)
    expect(clearLibraryFilters(filtered)).toEqual({ ...DEFAULT_LIBRARY_VIEW, sortKey: 'gate' })
  })

  it('tickery z dat, vybraný zůstane i po zmizení z dat', () => {
    expect(libraryTickers(SERVER, null)).toEqual(['ES', 'NQ'])
    expect(libraryTickers(SERVER, 'NQZ6')).toEqual(['ES', 'NQ', 'NQZ6'])
  })

  it('uložený pohled: platná pole se převezmou, neplatná spadnou na výchozí', () => {
    const stored = {
      sortKey: 'avgNetR',
      sortDir: 'asc',
      ticker: 'NQZ6',
      stages: ['trial', 'auto', 'trial', 'nesmysl'],
      gate: 'pass',
      query: 'trend',
      netPositive: true,
    }
    expect(reviveLibraryView(stored, DEFAULT_LIBRARY_VIEW)).toEqual({
      ...stored,
      stages: ['auto', 'trial'],
    })
    expect(
      reviveLibraryView(
        {
          sortKey: 'sum_r',
          sortDir: 'up',
          ticker: '../settings',
          stages: 'auto',
          gate: 'off',
          query: 5,
          netPositive: 'ano',
        },
        DEFAULT_LIBRARY_VIEW,
      ),
    ).toEqual(DEFAULT_LIBRARY_VIEW)
    // Řazení jen jako dvojice: zastaralý sloupec se směrem nesmí dát Průkaznost ↑
    expect(reviveLibraryView({ sortKey: 'sum_r', sortDir: 'asc' }, DEFAULT_LIBRARY_VIEW)).toEqual(
      DEFAULT_LIBRARY_VIEW,
    )
    expect(reviveLibraryView({ sortKey: 'ticker', sortDir: 'up' }, DEFAULT_LIBRARY_VIEW)).toEqual(
      DEFAULT_LIBRARY_VIEW,
    )
    expect(reviveLibraryView({ sortKey: 'ticker' }, DEFAULT_LIBRARY_VIEW)).toEqual(
      DEFAULT_LIBRARY_VIEW,
    )
    // Ticker a brána: uložené null = bez filtru, neplatné = výchozí
    const filtered = view({ ticker: 'NQ', gate: 'pass' })
    expect(reviveLibraryView({ ticker: null, gate: null }, filtered)).toMatchObject({
      ticker: null,
      gate: null,
    })
    expect(reviveLibraryView({ ticker: 'nq', gate: 'off' }, filtered)).toMatchObject({
      ticker: 'NQ',
      gate: 'pass',
    })
    expect(reviveLibraryView(null, DEFAULT_LIBRARY_VIEW)).toBe(DEFAULT_LIBRARY_VIEW)
    expect(reviveLibraryView([], DEFAULT_LIBRARY_VIEW)).toBe(DEFAULT_LIBRARY_VIEW)
    const long = reviveLibraryView({ query: 'x'.repeat(500) }, DEFAULT_LIBRARY_VIEW)
    expect(long.query).toHaveLength(100)
  })

  it('volby z výběru: jen známé hodnoty, „vše" a neznámé = null', () => {
    expect(librarySortKeyOf('avgNetR')).toBe('avgNetR')
    expect(librarySortKeyOf('sum_r')).toBeNull()
    expect(librarySortKeyOf(undefined)).toBeNull()
    expect(libraryGateFilterOf('block')).toBe('block')
    expect(libraryGateFilterOf('')).toBeNull()
    expect(libraryGateFilterOf('off')).toBeNull()
  })

  it('tooltip záhlaví v odrážkách', () => {
    const lines = librarySortTooltip('gate').split('\n')
    expect(lines[0]).toContain('block → nedostatek vzorku → pass')
    expect(lines.slice(1).every((line) => line.startsWith('• '))).toBe(true)
  })
})
