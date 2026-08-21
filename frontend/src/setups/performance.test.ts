/** Testy metriky výkonu (#794 fáze 0, ADR-0030) — kontrakt pro budoucí smyčku. */
import { expect, test } from 'vitest'
import type { SetupRow } from '../api/setups'
import {
  annualizedSharpe,
  currentMechanicsVersion,
  dailyRSeries,
  equityCurve,
  maxDrawdownOf,
  usdSimulation,
} from './performance'

/** Seance = UTC datum — testům stačí deterministické mapování. */
const toSession = (ts: number) => new Date(ts).toISOString().slice(0, 10)

function row(overrides: Partial<SetupRow>): SetupRow {
  return {
    id: 1,
    symbol: 'ES',
    expiry: '20260821',
    template: 'wall_bounce',
    direction: 'long',
    created_ts: '2026-08-20T10:00:00Z',
    entry: 6400,
    target: 6420,
    stop: 6390,
    confidence: 55,
    reason: '',
    status: 'closed_target',
    closed_ts: '2026-08-20T12:00:00Z',
    outcome_r: 2,
    mfe: null,
    mae: null,
    user_rating: null,
    user_note: null,
    mechanics_version: 4,
    ...overrides,
  }
}

test('currentMechanicsVersion: nejvyšší v datech, default 1', () => {
  expect(currentMechanicsVersion([])).toBe(1)
  expect(currentMechanicsVersion([{ mechanics_version: 2 }, { mechanics_version: 4 }, {}])).toBe(4)
})

test('dailyRSeries: jen aktuální mechanika, ΣR per seance, chronologicky', () => {
  const rows = [
    row({ id: 1, outcome_r: 2 }),
    row({ id: 2, outcome_r: -1, closed_ts: '2026-08-20T15:00:00Z' }),
    row({ id: 3, outcome_r: 1.5, closed_ts: '2026-08-21T12:00:00Z' }),
    // Stará mechanika a aktivní setupy do řady nepatří
    row({ id: 4, outcome_r: 10, mechanics_version: 2 }),
    row({ id: 5, status: 'active', outcome_r: null, closed_ts: null }),
  ]

  const series = dailyRSeries(rows, toSession)

  expect(series).toEqual([
    { session: '2026-08-20', value: 1, trades: 2 },
    { session: '2026-08-21', value: 1.5, trades: 1 },
  ])
})

test('annualizedSharpe: mean/std(ddof=1) × √252; málo dat → null', () => {
  expect(annualizedSharpe([]).sharpe).toBeNull()
  expect(annualizedSharpe([{ session: 'a', value: 1, trades: 1 }]).sharpe).toBeNull()

  const series = [1, 2, 3].map((value, index) => ({ session: `d${index}`, value, trades: 1 }))
  const result = annualizedSharpe(series)
  // mean 2, std ddof=1 = 1 → 2 × √252
  expect(result.sharpe).toBeCloseTo(2 * Math.sqrt(252), 6)
  expect(result.days).toBe(3)
})

test('equityCurve + maxDrawdownOf: kumulativ a nejhlubší propad od vrcholu', () => {
  const curve = equityCurve([
    { session: 'a', value: 2, trades: 1 },
    { session: 'b', value: -3, trades: 1 },
    { session: 'c', value: 1, trades: 1 },
  ])
  expect(curve.map((point) => point.equity)).toEqual([2, -1, 0])
  expect(maxDrawdownOf(curve)).toBe(-3)
})

test('usdSimulation: micro sizing #679, náklady, přeskočené obchody', () => {
  const rows = [
    // stop 10 b → MES (5 $/b): floor(100 / 50) = 2 kontrakty
    row({ id: 1, outcome_r: 2, entry: 6400, stop: 6390 }),
    // stop 40 b → floor(100 / 200) = 0 kontraktů → přeskočeno
    row({ id: 2, outcome_r: 1, entry: 6400, stop: 6360 }),
  ]

  const sim = usdSimulation(rows, toSession, { accountUsd: 5000, riskPct: 2 })

  expect(sim).not.toBeNull()
  expect(sim?.skipped).toBe(1)
  expect(sim?.traded).toBe(1)
  // P/L = 2 kontrakty × 2R × 10 b × 5 $ − 2 × 2,49 $ nákladů
  expect(sim?.daily).toEqual([{ session: '2026-08-20', value: 200 - 4.98, trades: 1 }])
})

test('usdSimulation: bez účtu nebo rizika → null (kalkulačka nevyplněná)', () => {
  expect(usdSimulation([row({})], toSession, { accountUsd: 0, riskPct: 1 })).toBeNull()
  expect(usdSimulation([row({})], toSession, { accountUsd: 5000, riskPct: 0 })).toBeNull()
})
