/** Paper účet (#1187, ADR-0040): REST klient + sizing pro order ticket.

Risk vrstva blokuje na serveru (409 s důvodem); klient si jen předpočítá,
kolik kontraktů rozpočet dovolí, aby ticket nabídl správné číslo rovnou. */
import { API_BASE } from '../config'

export type PaperSide = 'long' | 'short'
export type PaperOrderType = 'market' | 'limit' | 'stop'
export type PaperStatus = 'working' | 'open' | 'closed' | 'cancelled' | 'rejected'

export interface PaperOrderRow {
  id: number
  symbol: string
  side: PaperSide
  qty: number
  order_type: PaperOrderType
  entry_price: number
  stop_price: number
  target_price: number | null
  status: PaperStatus
  created_ts: string
  filled_ts: string | null
  fill_price: number | null
  closed_ts: string | null
  exit_price: number | null
  exit_reason: string | null
  pnl_usd: number | null
  fees_usd: number | null
  r_multiple: number | null
  risk_usd: number
  point_value: number
  setup_key: string | null
  note: string | null
  close_requested: boolean
  mfe: number | null
  mae: number | null
}

export interface PaperAccount {
  id: number
  name: string
  equity_start: number
  equity: number
  halted: boolean
  halted_reason: string | null
  risk_pct: number
  risk_max_pct: number
  risk_budget_usd: number
  day_r: number
  week_r: number
  brake: string | null
  daily_brake_r: number
  weekly_brake_r: number
  open: PaperOrderRow[]
  working: PaperOrderRow[]
}

export interface PaperOrderDraft {
  symbol: string
  side: PaperSide
  qty: number
  order_type: PaperOrderType
  entry_price: number
  stop_price: number
  target_price: number | null
  setup_key: string | null
  note: string | null
  context?: Record<string, unknown>
}

export interface PaperBlock {
  block: string
  reason: string
  max_contracts?: number
}

export const BLOCK_LABELS: Record<string, string> = {
  kill_switch: 'kill switch je zapnutý',
  position_exists: 'na symbolu už je pozice nebo čekající order',
  daily_brake: 'denní brzda',
  weekly_brake: 'týdenní brzda',
  stop_over_budget: 'stop nad rozpočtem rizika',
  stop_over_cap: 'stop nad tvrdým stropem',
}

export async function fetchPaperAccount(): Promise<PaperAccount | null> {
  try {
    const response = await fetch(`${API_BASE}/paper/account`)
    if (!response.ok) return null
    const payload = (await response.json()) as Partial<PaperAccount> | null
    if (!payload || typeof payload.equity !== 'number') return null
    return {
      ...(payload as PaperAccount),
      open: payload.open ?? [],
      working: payload.working ?? [],
    }
  } catch {
    return null
  }
}

export type PlaceResult =
  { ok: true; order: PaperOrderRow } | { ok: false; block: PaperBlock | null; error: string }

async function errorText(response: Response): Promise<{ block: PaperBlock | null; error: string }> {
  const body = (await response.json().catch(() => ({}))) as { detail?: unknown }
  const detail = body.detail
  if (detail && typeof detail === 'object' && 'block' in detail) {
    const block = detail as PaperBlock
    return { block, error: `${BLOCK_LABELS[block.block] ?? block.block}: ${block.reason}` }
  }
  if (typeof detail === 'string') return { block: null, error: detail }
  if (Array.isArray(detail)) {
    const first = detail[0] as { msg?: string } | undefined
    return { block: null, error: first?.msg ?? `HTTP ${response.status}` }
  }
  return { block: null, error: `HTTP ${response.status}` }
}

export async function placePaperOrder(draft: PaperOrderDraft): Promise<PlaceResult> {
  try {
    const response = await fetch(`${API_BASE}/paper/orders`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(draft),
    })
    if (!response.ok) return { ok: false, ...(await errorText(response)) }
    return { ok: true, order: (await response.json()) as PaperOrderRow }
  } catch (error) {
    return { ok: false, block: null, error: error instanceof Error ? error.message : String(error) }
  }
}

/** Zruší čekající order / zavře pozici (engine provede na dalším baru). */
export async function closePaperOrder(id: number): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/paper/orders/${id}`, { method: 'DELETE' })
    return response.ok
  } catch {
    return false
  }
}

export async function killPaper(reason: string): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/paper/kill`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason }),
    })
    return response.ok
  } catch {
    return false
  }
}

export async function resumePaper(): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/paper/resume`, { method: 'POST' })
    return response.ok
  } catch {
    return false
  }
}

/** Kolik kontraktů rozpočet dovolí: ⌊rozpočet / (|entry − stop| × bod)⌋ (0 = stop moc daleko). */
export function maxContracts(
  budgetUsd: number,
  entry: number,
  stop: number,
  pointValue: number,
): number {
  const perContract = Math.abs(entry - stop) * pointValue
  if (!(perContract > 0) || !(budgetUsd > 0)) return 0
  return Math.floor(budgetUsd / perContract)
}

/** Ztráta na stopu v $ pro daný počet kontraktů. */
export function riskUsd(qty: number, entry: number, stop: number, pointValue: number): number {
  return Math.abs(entry - stop) * pointValue * qty
}

/** RRR z úrovní; null bez cíle. */
export function rewardRisk(entry: number, stop: number, target: number | null): number | null {
  const risk = Math.abs(entry - stop)
  if (target === null || !(risk > 0)) return null
  return Math.abs(target - entry) / risk
}

/** Text pozice do chipu: „LONG 1× ES @ 7600 · stop 7592 · cíl 7616". */
export function orderLabel(order: PaperOrderRow): string {
  const price = order.fill_price ?? order.entry_price
  const state = order.status === 'open' ? '' : ` (${order.order_type} čeká)`
  return (
    `${order.side.toUpperCase()} ${order.qty}× ${order.symbol} @ ${price}${state}` +
    ` · stop ${order.stop_price}` +
    (order.target_price !== null ? ` · cíl ${order.target_price}` : '')
  )
}
