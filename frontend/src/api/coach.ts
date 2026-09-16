/** Kouč v1 (#1187 fáze 3, #933): REST klient denního review a týdenního reportu.

Server (`compute/coach.py`) dává každému obchodu deníku příznaky s důkazem
a cenou v R, skóre disciplíny dne a 1–3 pravidla na příští týden. UI jen
zobrazuje. */
import { API_BASE } from '../config'

export interface CoachFlag {
  kind: string
  label: string
  detail: string
  cost_r: number | null
}

export interface CoachTrade {
  id: number
  symbol: string
  direction: 'long' | 'short'
  opened_ts: string | null
  closed_ts: string | null
  setup_key: string | null
  paper: boolean
  exit_reason: string | null
  realized_r: number | null
  planned_rr: number | null
  capture: number | null
  net_pnl: number | null
  flags: CoachFlag[]
  penalty: number
}

export interface CoachDaily {
  session_day: string
  rules_version: number
  trades: CoachTrade[]
  n: number
  score: number
  total_r: number
  flagged_cost_r: number
  summary: string
}

export interface CoachRule {
  kind: string
  advice: string
  n: number
  cost_r: number
}

export interface CoachWeekly {
  week_start: string
  week_end: string
  rules_version: number
  n: number
  total_r: number
  win_rate: number | null
  avg_r: number | null
  score: number | null
  capture: number | null
  flags: Record<string, { n: number; cost_r: number }>
  by_hour_utc: Record<string, { n: number; sum_r: number }>
  days: CoachDaily[]
  rules: CoachRule[]
}

export interface CoachBucket {
  n: number
  sum_r: number
  avg_r: number
  win_rate: number
  win_lb: number
}

export interface CoachTimeProfile {
  n?: number
  tz: string
  hours: Record<string, CoachBucket>
  segments: Record<string, CoachBucket & { label: string }>
  best_hour: number | null
  worst_hour: number | null
  best_segment: string | null
  worst_segment: string | null
  min_window_sample: number
}

export interface CoachHours {
  days: number
  symbol: string | null
  trades: CoachTimeProfile & { n: number }
  setups: CoachTimeProfile & { n: number }
}

export interface CoachRecommendation extends CoachBucket {
  kind: 'avoid' | 'focus'
  scope: string
  key: string
  text: string
}

export interface CoachSetupsReport {
  rules_version: number
  days: number
  symbol: string | null
  n: number
  total_r: number
  min_sample: number
  by_template: Record<string, CoachBucket>
  time: CoachTimeProfile
  flags: Record<string, { label: string; n: number; sum_r: number }>
  recommendations: CoachRecommendation[]
}

function isHours(value: unknown): value is CoachHours {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as CoachHours).trades === 'object' &&
    typeof (value as CoachHours).setups === 'object'
  )
}

function isSetupsReport(value: unknown): value is CoachSetupsReport {
  return (
    typeof value === 'object' &&
    value !== null &&
    Array.isArray((value as CoachSetupsReport).recommendations) &&
    typeof (value as CoachSetupsReport).time === 'object'
  )
}

export function fetchCoachHours(days = 60, symbol?: string): Promise<CoachHours | null> {
  const params = new URLSearchParams({ days: String(days) })
  if (symbol) params.set('symbol', symbol)
  return getJson(`${API_BASE}/coach/hours?${params.toString()}`, isHours)
}

export function fetchCoachSetups(days = 60, symbol?: string): Promise<CoachSetupsReport | null> {
  const params = new URLSearchParams({ days: String(days) })
  if (symbol) params.set('symbol', symbol)
  return getJson(`${API_BASE}/coach/setups?${params.toString()}`, isSetupsReport)
}

/** Řádky profilu denní doby seřazené podle Ø R; jen koše se vzorkem (min n). */
export function rankedWindows(
  profile: CoachTimeProfile,
  minN = profile.min_window_sample,
): Array<{ key: string; label: string; bucket: CoachBucket }> {
  const rows = Object.entries(profile.segments)
    .filter(([, bucket]) => bucket.n >= minN)
    .map(([key, bucket]) => ({ key, label: bucket.label, bucket }))
  return rows.sort((a, b) => b.bucket.avg_r - a.bucket.avg_r)
}

async function getJson<T>(url: string, guard: (value: unknown) => value is T): Promise<T | null> {
  try {
    const response = await fetch(url)
    if (!response.ok) return null
    const payload: unknown = await response.json()
    return guard(payload) ? payload : null
  } catch {
    return null
  }
}

function isDaily(value: unknown): value is CoachDaily {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as CoachDaily).session_day === 'string' &&
    Array.isArray((value as CoachDaily).trades)
  )
}

function isWeekly(value: unknown): value is CoachWeekly {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as CoachWeekly).week_start === 'string' &&
    Array.isArray((value as CoachWeekly).rules)
  )
}

export function fetchCoachReview(date?: string, symbol?: string): Promise<CoachDaily | null> {
  const params = new URLSearchParams()
  if (date) params.set('date', date)
  if (symbol) params.set('symbol', symbol)
  const query = params.toString()
  return getJson(`${API_BASE}/coach/review${query ? `?${query}` : ''}`, isDaily)
}

export function fetchCoachWeekly(date?: string, symbol?: string): Promise<CoachWeekly | null> {
  const params = new URLSearchParams()
  if (date) params.set('date', date)
  if (symbol) params.set('symbol', symbol)
  const query = params.toString()
  return getJson(`${API_BASE}/coach/weekly${query ? `?${query}` : ''}`, isWeekly)
}

export function formatR(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '—'
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)} R`
}

/** Barva skóre: ≥ 85 zelená, ≥ 60 žlutá, jinak červená. */
export function scoreTone(score: number | null): 'good' | 'warn' | 'bad' | 'none' {
  if (score === null) return 'none'
  if (score >= 85) return 'good'
  if (score >= 60) return 'warn'
  return 'bad'
}
