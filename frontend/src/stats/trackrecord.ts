/** Souhrn track recordu pro záložku Stats (#298, SPEC 7.3) — čisté funkce. */
import type { SignalRow, TrackRecordRow } from '../api/news'

export const STRATEGY_LABELS: Record<string, string> = {
  buy_hold: 'Buy & hold',
  state: 'Stavová (RiskOn long)',
  signals_news: 'Signály NEWS',
  signals_combined: 'Signály COMBINED',
}

export const STRATEGY_COLORS: Record<string, string> = {
  buy_hold: '#7d8596',
  state: '#e8c14b',
  signals_news: '#14b8a6',
  signals_combined: '#4da3ff',
}

/** Křivky per strategie, seřazené datem; jen zvolený symbol. */
export function groupCurves(rows: TrackRecordRow[], symbol: string): Map<string, TrackRecordRow[]> {
  const curves = new Map<string, TrackRecordRow[]>()
  for (const row of rows) {
    if (row.symbol !== symbol) continue
    const list = curves.get(row.strategy)
    if (list) list.push(row)
    else curves.set(row.strategy, [row])
  }
  for (const list of curves.values()) {
    list.sort((a, b) => (a.date < b.date ? -1 : 1))
  }
  return curves
}

/** CAGR z equity křivky (roční složený výnos); null pro < 2 body. */
export function cagr(curve: TrackRecordRow[]): number | null {
  if (curve.length < 2) return null
  const first = curve[0]
  const last = curve[curve.length - 1]
  const days = (new Date(last.date).getTime() - new Date(first.date).getTime()) / 86_400_000
  if (days <= 0 || first.equity <= 0 || last.equity <= 0) return null
  return (last.equity / first.equity) ** (365 / days) - 1
}

/** Nejhlubší drawdown křivky (záporné číslo; 0 = bez poklesu). */
export function maxDrawdown(curve: TrackRecordRow[]): number {
  let worst = 0
  for (const row of curve) {
    if (row.drawdown !== null && row.drawdown < worst) worst = row.drawdown
  }
  return worst
}

/** Hit-rate signálové strategie z outcomes na primárním okně (+5 min).

Jen signálové strategie — u stavové a buy & hold není obchod, nad kterým by
hit-rate dávala smysl (ADR-0021). */
export function signalHitRate(
  signals: SignalRow[],
  mode: 'NEWS' | 'COMBINED',
  windowMin = 5,
): { hits: number; total: number } {
  let hits = 0
  let total = 0
  for (const signal of signals) {
    if (signal.mode !== mode) continue
    const outcome = signal.outcomes?.find((o) => o.window_min === windowMin)
    if (!outcome || outcome.correct === null) continue
    total += 1
    if (outcome.correct) hits += 1
  }
  return { hits, total }
}
