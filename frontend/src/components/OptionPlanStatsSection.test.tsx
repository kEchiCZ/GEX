/** Track record plánu opce QQQ ve Stats (F5b #1749): prázdný stav, předběžné, plný stav, stíny
mimo hlavní plán, výslovná chyba. Čísla jsou syntetická (žádné hodnoty účtu). */
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { OptionPlanStatsSection } from './OptionPlanStatsSection'
import type {
  OptionPlanGroup,
  OptionPlanPnl,
  OptionPlanSection,
  OptionPlanStats,
} from '../api/optionPlans'

const OUTCOMES = ['SL', 'TP1', 'TP2', 'TP1→vstup', 'TP1→exit', 'exit', 'cíl 1 za vstupem']
const BUDGETS = ['v rozpočtu', 'nad rozpočtem', 'nenastaven']

function pnl(n = 0, sum = 0, mean: number | null = null, wins = 0): OptionPlanPnl {
  return { n, sum_usd: sum, mean_usd: mean, wins }
}

function counts(given: Record<string, number>): Record<string, number> {
  return Object.fromEntries(OUTCOMES.map((name) => [name, given[name] ?? 0]))
}

function leaf(
  n = 0,
  outcomes: Record<string, number> = {},
  tp1 = pnl(),
  hold = pnl(),
  preliminary = n < 30,
): OptionPlanGroup {
  return { n, preliminary, outcomes: counts(outcomes), pnl_tp1: tp1, pnl_hold: hold }
}

function withBudget(top: OptionPlanGroup, parts: Record<string, OptionPlanGroup> = {}) {
  return {
    ...top,
    by_budget: Object.fromEntries(BUDGETS.map((budget) => [budget, parts[budget] ?? leaf()])),
  }
}

function section(
  plans: number,
  status: Record<string, number>,
  variants: Partial<Record<'hlavní' | 'záložní', Partial<OptionPlanSection['variants'][string]>>>,
  preliminary = plans < 30,
): OptionPlanSection {
  const empty = { trades: withBudget(leaf()), rejected_p3: withBudget(leaf()), no_quote: 0 }
  return {
    plans,
    preliminary,
    by_status: {
      spuštěno: status['spuštěno'] ?? 0,
      'bez vstupu': status['bez vstupu'] ?? 0,
      'bez opce': status['bez opce'] ?? 0,
      mezera: status['mezera'] ?? 0,
    },
    variants: {
      hlavní: { ...empty, ...variants['hlavní'] },
      záložní: { ...empty, ...variants['záložní'] },
    },
  }
}

const EMPTY_SECTION = section(0, {}, {})

function stats(overrides: Partial<OptionPlanStats>): OptionPlanStats {
  return {
    rules_version: 1,
    since: '2026-12-01',
    outside: 0,
    main: EMPTY_SECTION,
    shadows: { entry_b: EMPTY_SECTION, wick_test: EMPTY_SECTION },
    ...overrides,
  }
}

function mockFetch(body: unknown, status = 200) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) })),
  )
}

function cells(testId: string): string[] {
  return Array.from(screen.getByTestId(testId).querySelectorAll('td')).map(
    (cell) => cell.textContent ?? '',
  )
}

beforeEach(() => {
  vi.unstubAllGlobals()
})

test('u ES poznámka, že plán ze scénáře ES nevzniká (#1790), bez dotazu na API', () => {
  mockFetch(stats({}))
  render(<OptionPlanStatsSection symbol="ES" />)
  expect(screen.getByTestId('option-plan-stats-other-symbol').textContent).toContain('#1790')
  expect(globalThis.fetch).not.toHaveBeenCalled()
})

test('prázdný stav před začátkem track recordu: počet plánů mimo, žádná tabulka', async () => {
  mockFetch(stats({ since: null, outside: 3 }))
  render(<OptionPlanStatsSection symbol="NQ" />)
  const empty = await screen.findByTestId('option-plan-stats-empty')
  expect(empty.textContent).toContain('Track record zatím nezačal')
  expect(empty.textContent).toContain('mimo track record: 3')
  expect(screen.queryByTestId('option-plan-stats')).toBeNull()
  expect(String(vi.mocked(globalThis.fetch).mock.calls[0][0])).toContain('/option-plans/stats')
})

test('předběžné při n < 30: obchody, vyřazené P3 hypoteticky, jen neprázdné třídy rozpočtu', async () => {
  const trades = leaf(2, { SL: 1, TP2: 1 }, pnl(2, 30, 15, 1), pnl(2, 70, 35, 1))
  const rejected = leaf(1, { TP2: 1 }, pnl(1, 20, 20, 1), pnl(1, 50, 50, 1))
  mockFetch(
    stats({
      outside: 2,
      main: section(
        4,
        { spuštěno: 3, 'bez vstupu': 1, mezera: 1 },
        {
          hlavní: {
            trades: withBudget(trades, { 'v rozpočtu': trades }),
            rejected_p3: withBudget(rejected, { 'nad rozpočtem': rejected }),
          },
          záložní: { no_quote: 1 },
        },
      ),
    }),
  )
  render(<OptionPlanStatsSection symbol="NQ" />)
  await screen.findByTestId('option-plan-stats')
  expect(screen.getByTestId('option-plan-stats-since').textContent).toContain('2026-12-01')

  const head = screen.getByTestId('option-plan-part-main').querySelector('th')?.textContent ?? ''
  expect(head).toContain('posouzeno 4 (spuštěno 3 · bez vstupu 1)')
  expect(head).toContain('mezera 1')
  expect(head).toContain('předběžné')

  const row = cells('option-plan-main-hlavní-trades')
  expect(row[0]).toBe('hlavní · obchody')
  expect(row[1]).toBe('2')
  expect(row[2]).toBe('1') // SL
  expect(row[4]).toBe('1') // TP2
  expect(row).toContain('Σ +30 $ · ⌀ +15 $ · zisk 1/2')
  expect(row).toContain('Σ +70 $ · ⌀ +35 $ · zisk 1/2')
  expect(row[row.length - 1]).toBe('předběžné')

  // Třídy rozpočtu: jen neprázdné
  expect(screen.getByTestId('option-plan-main-hlavní-trades-budget-v rozpočtu')).toBeTruthy()
  expect(screen.queryByTestId('option-plan-main-hlavní-trades-budget-nad rozpočtem')).toBeNull()
  expect(screen.queryByTestId('option-plan-main-hlavní-trades-budget-nenastaven')).toBeNull()

  // Vyřazené P3: vlastní řádek s hypotetickým výsledkem, ztlumený
  const rejectedRow = screen.getByTestId('option-plan-main-hlavní-rejected')
  expect(rejectedRow.className).toContain('option-plan-rejected')
  expect(cells('option-plan-main-hlavní-rejected')[0]).toBe('hlavní · vyřazeno P3 — hypoteticky')
  expect(cells('option-plan-main-hlavní-rejected')).toContain('Σ +20 $ · ⌀ +20 $ · zisk 1/1')
  const rejectedBudget = cells('option-plan-main-hlavní-rejected-budget-nad rozpočtem')
  expect(rejectedBudget[0]).toBe('↳ nad rozpočtem')

  // Bez kotace jen tam, kde je; bez P&L jen pomlčka
  expect(cells('option-plan-main-záložní-no-quote')[1]).toBe('1')
  expect(screen.queryByTestId('option-plan-main-hlavní-no-quote')).toBeNull()
  expect(cells('option-plan-main-záložní-trades')).toContain('—')
})

test('plný stav n ≥ 30: platné', async () => {
  const trades = leaf(30, { TP2: 18, SL: 12 }, pnl(30, 600, 20, 18), pnl(30, 900, 30, 18), false)
  mockFetch(
    stats({
      main: section(
        32,
        { spuštěno: 30, 'bez vstupu': 2 },
        { hlavní: { trades: withBudget(trades) } },
      ),
    }),
  )
  render(<OptionPlanStatsSection symbol="NQ" />)
  await screen.findByTestId('option-plan-stats')
  const row = cells('option-plan-main-hlavní-trades')
  expect(row[1]).toBe('30')
  expect(row[row.length - 1]).toBe('platné')
  expect(row).toContain('Σ +600 $ · ⌀ +20 $ · zisk 18/30')
  const head = screen.getByTestId('option-plan-part-main').querySelector('th')?.textContent ?? ''
  expect(head).toContain('platné')
})

test('stíny K10 B: samostatné řádky se štítkem stín, do hlavního plánu se nepočítají', async () => {
  const mainTrades = leaf(1, { TP2: 1 }, pnl(1, 10, 10, 1), pnl(1, 15, 15, 1))
  const shadowTrades = leaf(4, { SL: 4 }, pnl(4, -400, -100, 0), pnl(4, -400, -100, 0))
  mockFetch(
    stats({
      main: section(1, { spuštěno: 1 }, { hlavní: { trades: withBudget(mainTrades) } }),
      shadows: {
        entry_b: section(
          4,
          { spuštěno: 4 },
          {
            hlavní: { trades: withBudget(shadowTrades, { nenastaven: shadowTrades }) },
          },
        ),
        wick_test: EMPTY_SECTION,
      },
    }),
  )
  render(<OptionPlanStatsSection symbol="NQ" />)
  await screen.findByTestId('option-plan-stats')

  // Hlavní plán nese jen svá čísla
  const main = cells('option-plan-main-hlavní-trades')
  expect(main[1]).toBe('1')
  expect(main).toContain('Σ +10 $ · ⌀ +10 $ · zisk 1/1')
  const mainPart = screen.getByTestId('option-plan-part-main').textContent ?? ''
  expect(mainPart).not.toContain('-400')
  expect(mainPart).not.toContain('stín')

  // Stín vstupu B: vlastní blok, štítek, ztlumené řádky, stejná struktura včetně rozpočtu (Q3 B)
  const shadowPart = screen.getByTestId('option-plan-part-entry_b')
  expect(shadowPart.querySelector('th')?.textContent).toContain('Stín · vstup B')
  const shadowRow = screen.getByTestId('option-plan-entry_b-hlavní-trades')
  expect(shadowRow.className).toContain('option-plan-shadow')
  expect(cells('option-plan-entry_b-hlavní-trades')[0]).toBe('stín · hlavní · obchody')
  expect(cells('option-plan-entry_b-hlavní-trades')).toContain('Σ -400 $ · ⌀ -100 $ · zisk 0/4')
  expect(cells('option-plan-entry_b-hlavní-trades-budget-nenastaven')[0]).toBe(
    '↳ rozpočet nenastaven',
  )
  expect(
    screen.getByTestId('option-plan-part-wick_test').querySelector('th')?.textContent,
  ).toContain('Stín · test knotem')
})

test('chyba čtení je výslovná s kódem, ne prázdná tabulka', async () => {
  mockFetch({}, 503)
  render(<OptionPlanStatsSection symbol="NQ" />)
  const error = await screen.findByTestId('option-plan-stats-error')
  expect(error.textContent).toBe('Track record plánu se nepodařilo načíst (HTTP 503).')
  expect(screen.queryByTestId('option-plan-stats')).toBeNull()
})

test('neočekávaný tvar odpovědi je chyba, ne nuly', async () => {
  mockFetch({ since: null })
  render(<OptionPlanStatsSection symbol="NQ" />)
  await waitFor(() =>
    expect(screen.getByTestId('option-plan-stats-error').textContent).toContain(
      'neočekávaný tvar odpovědi',
    ),
  )
})
