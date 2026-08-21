/** Výkon setupů (#794 fáze 0, ADR-0030): denní ΣR, USD simulace, Sharpe.

Čisté funkce nad uzavřenými setupy AKTUÁLNÍ mechaniky. Den = obchodní seance
(#512), Sharpe = mean/std denních výnosů × √252. USD větev simuluje exekuci
micro kontrakty dle kalkulačky #679 včetně nákladů — floor a přeskočené
obchody (0 kontraktů) jsou realita, kterou R-řada nevidí.
*/
import type { SetupRow } from '../api/setups'
import { MICRO_SYMBOLS } from '../instrument/position'
import { pointValue } from '../instrument/tick'

/** Aktuální verze mechaniky = nejvyšší v datech (ADR-0030).

Engine jinou nevyrábí; natvrdo zapsaná konstanta už jednou zastarala
(v2 ve frontendu vs. v4 v enginu — týden statistik bez aktuálních setupů). */
export function currentMechanicsVersion(rows: Pick<SetupRow, 'mechanics_version'>[]): number {
  let version = 1
  for (const row of rows) {
    const value = row.mechanics_version ?? 1
    if (value > version) version = value
  }
  return version
}

/** Náklad round-trip na 1 kontrakt v USD (ADR-0030): komise 2×0,62 $ + 1 tick
slippage na stranu. Konstanty do doby, než je nahradí měřená exekuce. */
export const ROUND_TRIP_COST_USD: Record<string, number> = {
  MES: 2.49,
  MNQ: 1.74,
}
const DEFAULT_ROUND_TRIP_COST_USD = 2.5

export function roundTripCostUsd(symbol: string): number {
  return ROUND_TRIP_COST_USD[symbol] ?? DEFAULT_ROUND_TRIP_COST_USD
}

/** Uzavřený obchod přepočtený na seanci — společný vstup R i USD řady. */
interface ClosedTrade {
  session: string
  r: number
  symbol: string
  stopPoints: number
}

/** Uzavřené obchody aktuální mechaniky, chronologicky, se seancí uzavření. */
export function closedTrades(
  rows: SetupRow[],
  toSessionDate: (ts: number) => string,
): ClosedTrade[] {
  const version = currentMechanicsVersion(rows)
  return rows
    .filter(
      (row) =>
        (row.mechanics_version ?? 1) === version &&
        row.status !== 'active' &&
        row.outcome_r !== null &&
        row.closed_ts !== null,
    )
    .sort(
      (a, b) =>
        new Date(a.closed_ts as string).getTime() - new Date(b.closed_ts as string).getTime(),
    )
    .map((row) => ({
      session: toSessionDate(new Date(row.closed_ts as string).getTime()),
      r: row.outcome_r as number,
      symbol: row.symbol,
      stopPoints: Math.abs(row.entry - row.stop),
    }))
}

export interface DailyPoint {
  session: string
  value: number
  trades: number
}

/** Denní řada: ΣhodnotA per seance. Seance bez obchodu se NEpřidávají —
nula by uměle snižovala volatilitu (ADR-0030: absence pozorování ≠ výnos 0). */
function dailySeries(values: { session: string; value: number }[]): DailyPoint[] {
  const bySession = new Map<string, { value: number; trades: number }>()
  for (const item of values) {
    const entry = bySession.get(item.session) ?? { value: 0, trades: 0 }
    entry.value += item.value
    entry.trades += 1
    bySession.set(item.session, entry)
  }
  return [...bySession.entries()]
    .map(([session, entry]) => ({ session, value: entry.value, trades: entry.trades }))
    .sort((a, b) => a.session.localeCompare(b.session))
}

/** Denní ΣR řada aktuální mechaniky (primární metrika smyčky, ADR-0030). */
export function dailyRSeries(
  rows: SetupRow[],
  toSessionDate: (ts: number) => string,
): DailyPoint[] {
  return dailySeries(
    closedTrades(rows, toSessionDate).map((trade) => ({
      session: trade.session,
      value: trade.r,
    })),
  )
}

export interface SharpeResult {
  /** Anualizovaný Sharpe (×√252); null při <2 seancích nebo nulové volatilitě. */
  sharpe: number | null
  days: number
  mean: number
  std: number | null
}

/** Anualizovaný Sharpe z denní řady: mean/std(ddof=1) × √252 (ADR-0030). */
export function annualizedSharpe(series: DailyPoint[]): SharpeResult {
  const values = series.map((point) => point.value)
  const days = values.length
  if (days < 2) return { sharpe: null, days, mean: values[0] ?? 0, std: null }
  const mean = values.reduce((sum, value) => sum + value, 0) / days
  const variance = values.reduce((sum, value) => sum + (value - mean) ** 2, 0) / (days - 1)
  const std = Math.sqrt(variance)
  if (std === 0) return { sharpe: null, days, mean, std: 0 }
  return { sharpe: (mean / std) * Math.sqrt(252), days, mean, std }
}

/** Kumulativní equity z denní řady (od nuly, bod za každou seanci). */
export function equityCurve(series: DailyPoint[]): { session: string; equity: number }[] {
  let equity = 0
  return series.map((point) => {
    equity += point.value
    return { session: point.session, equity }
  })
}

/** Max drawdown kumulativní equity (záporné číslo v jednotkách řady, 0 = žádný). */
export function maxDrawdownOf(curve: { equity: number }[]): number {
  let peak = 0
  let worst = 0
  for (const point of curve) {
    if (point.equity > peak) peak = point.equity
    const dd = point.equity - peak
    if (dd < worst) worst = dd
  }
  return worst
}

export interface UsdSimulation {
  daily: DailyPoint[]
  /** Obchody přeskočené kalkulačkou (0 kontraktů) — realita malého účtu. */
  skipped: number
  traded: number
}

/** USD simulace s exekucí micro kontrakty a náklady (ADR-0030, sizing #679).

Kontrakty = floor(účet × riziko% / (stop v bodech × hodnota bodu micro));
P/L = kontrakty × R × stop × bod − kontrakty × round-trip náklad. */
export function usdSimulation(
  rows: SetupRow[],
  toSessionDate: (ts: number) => string,
  input: { accountUsd: number; riskPct: number },
): UsdSimulation | null {
  if (input.accountUsd <= 0 || input.riskPct <= 0) return null
  const riskUsd = (input.accountUsd * input.riskPct) / 100
  let skipped = 0
  const perTrade: { session: string; value: number }[] = []
  for (const trade of closedTrades(rows, toSessionDate)) {
    const micro = MICRO_SYMBOLS[trade.symbol] ?? trade.symbol
    const point = pointValue(micro)
    if (trade.stopPoints <= 0) {
      skipped += 1
      continue
    }
    const contracts = Math.floor(riskUsd / (trade.stopPoints * point))
    if (contracts <= 0) {
      skipped += 1
      continue
    }
    const pnl = contracts * trade.r * trade.stopPoints * point - contracts * roundTripCostUsd(micro)
    perTrade.push({ session: trade.session, value: pnl })
  }
  return { daily: dailySeries(perTrade), skipped, traded: perTrade.length }
}
