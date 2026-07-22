/** Setup detektor (ADR-0004): REST klient a české popisky šablon. */
import { API_BASE } from '../config'

export interface SetupRow {
  id: number
  symbol: string
  expiry: string
  template: string
  direction: 'long' | 'short'
  created_ts: string
  entry: number
  target: number
  stop: number
  confidence: number
  reason: string
  status: 'active' | 'closed_target' | 'closed_stop' | 'closed_timeout'
  closed_ts: string | null
  outcome_r: number | null
  mfe: number | null
  mae: number | null
  user_rating: number | null
  user_note: string | null
}

export const TEMPLATE_LABELS: Record<string, string> = {
  wall_bounce: 'Odraz od zdi',
  failed_break: 'Neúspěšný průraz',
  max_pain_pin: 'Max Pain pin',
  gamma_momentum: 'Gamma momentum',
}

export const STATUS_LABELS: Record<SetupRow['status'], string> = {
  active: 'Aktivní',
  closed_target: 'Cíl',
  closed_stop: 'Stop',
  closed_timeout: 'Timeout',
}

export function templateLabel(template: string): string {
  return TEMPLATE_LABELS[template] ?? template
}

/** RRR z uložených úrovní (predikce je neměnná — počítá se ze setupu, ne z běhu). */
export function setupRrr(row: Pick<SetupRow, 'entry' | 'target' | 'stop'>): number {
  const risk = Math.abs(row.entry - row.stop)
  return risk > 0 ? Math.abs(row.target - row.entry) / risk : 0
}

/** P/L uzavřeného setupu v USD na 1 KONTRAKT (#185).

`outcome_r` je výsledek v násobcích rizika; riziko v bodech = |entry − stop|,
takže P/L body = outcome_r × riziko a dolary přes hodnotu bodu instrumentu.
Platí i pro timeout (engine počítá outcome_r z exit ceny). */
export function setupPnlUsd(
  row: Pick<SetupRow, 'entry' | 'stop' | 'outcome_r'>,
  pointValueUsd: number,
): number | null {
  if (row.outcome_r === null) return null
  return row.outcome_r * Math.abs(row.entry - row.stop) * pointValueUsd
}

/** Formát P/L se znaménkem („+512.50 $" / „−250 $"). */
export function formatPnlUsd(value: number): string {
  const rounded = Math.round(value * 100) / 100
  return `${rounded > 0 ? '+' : ''}${rounded} $`
}

/** Startovní kapitál účtu na jeden ticker (#191, zadání uživatele) — báze procent. */
export const ACCOUNT_START_USD = 5000

/** P/L setupu v % startovního účtu (#191): pnl $ / 5 000 $ na ticker.

S fixní bází je součet procent setupů roven celkovému zhodnocení účtu. */
export function setupPnlPct(
  row: Pick<SetupRow, 'entry' | 'stop' | 'outcome_r'>,
  pointValueUsd: number,
): number | null {
  const pnl = setupPnlUsd(row, pointValueUsd)
  if (pnl === null) return null
  return (pnl / ACCOUNT_START_USD) * 100
}

/** Formát procenta se znaménkem („+0.19 %"). */
export function formatPct(value: number): string {
  return `${value > 0 ? '+' : ''}${value.toFixed(2)} %`
}

export async function fetchSetups(symbol: string): Promise<SetupRow[]> {
  const response = await fetch(`${API_BASE}/setups/${symbol}`)
  if (!response.ok) return []
  const payload = (await response.json()) as { setups?: SetupRow[] }
  return payload.setups ?? []
}

/** Ruční hodnocení uzavřeného setupu (kvalitativní vrstva — nevstupuje do kalibrace). */
export async function reviewSetup(
  symbol: string,
  id: number,
  rating: 1 | -1 | null,
  note: string | null,
): Promise<boolean> {
  const response = await fetch(`${API_BASE}/setups/${symbol}/${id}/review`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rating, note }),
  })
  return response.ok
}
