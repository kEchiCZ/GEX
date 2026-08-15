/** Deník tradera (#673 fáze A, #709 rev. 2): REST klient /journal. */
import { API_BASE } from '../config'

export type JournalType = 'pozorovani' | 'hypoteza' | 'retro_dne' | 'obchod' | 'promeskane'

export const JOURNAL_TYPE_LABELS: Record<JournalType, string> = {
  pozorovani: 'Pozorování',
  hypoteza: 'Hypotéza',
  retro_dne: 'Retrospektiva dne',
  obchod: 'Obchod',
  promeskane: 'Promeškaný setup',
}

/** Proč jsem platný setup nevzal (#715) — zrcadlo `MISSED_REASONS`. */
export const MISSED_REASON_LABELS: Record<string, string> = {
  nevsiml_jsem_si: 'Nevšiml jsem si',
  nedovera: 'Nedůvěra k setupu',
  mimo_plan: 'Mimo plán',
  mimo_seanci: 'Mimo mou seanci',
  risk_vycerpan: 'Risk už vyčerpaný',
  vahani: 'Váhání',
}

export const MISSED_REASONS = Object.keys(MISSED_REASON_LABELS)

/** Profil deníku (#709) — řídí, která pole formulář ukazuje. */
export type JournalProfile = 'smb' | 'futures'

export const JOURNAL_PROFILE_LABELS: Record<JournalProfile, string> = {
  smb: 'SMB',
  futures: 'Futures',
}

/** Symboly, u kterých se předvyplňuje futures profil (zrcadlo `meta.py`). */
const FUTURES_SYMBOLS = new Set(['ES', 'NQ', 'MES', 'MNQ', 'RTY', 'YM', 'M2K', 'MYM'])

export function defaultProfile(symbol: string): JournalProfile {
  return FUTURES_SYMBOLS.has(symbol.toUpperCase()) ? 'futures' : 'smb'
}

export type TradeDirection = 'long' | 'short'
export type JournalGrade = 'A' | 'B' | 'C'

export interface JournalTrade {
  direction: TradeDirection
  planned_entry: number | null
  planned_stop: number | null
  planned_target: number | null
  actual_entry: number | null
  actual_exit: number | null
  size: number | null
  opened_ts: string | null
  closed_ts: string | null
  setup_key: string | null
  /** Proč teze selhala (#711) — jen u ztrátových obchodů ve futures profilu. */
  failure_mode: string | null
  setup_grade: JournalGrade | null
  execution_grade: JournalGrade | null
  mistake_tags: string[]
  emotion: number | null
  mfe: number | null
  mae: number | null
  gross_pnl: number | null
  net_pnl: number | null
  fees: number | null
}

export interface JournalEntry {
  id: number
  ts_ref: string
  symbol: string
  entry_type: JournalType
  text: string
  tags: string[]
  setup_id: number | null
  news_event_id: number | null
  profile: JournalProfile
  trade: JournalTrade | null
  /** Snímek GEX kontextu k ts_ref (#711); null u záznamů z fáze A. */
  context: Record<string, unknown> | null
  /** Ranní plán / Daily Report Card (#712) u typu retro_dne. */
  daily: Record<string, unknown> | null
  /** Proč jsem setup nevzal (#715) u typu promeskane. */
  missed_reason: string | null
  created_ts: string
  updated_ts: string | null
}

/** Taxonomie selhání teze (#711) — zrcadlo `FAILURE_MODES` v meta.py. */
export const FAILURE_MODE_LABELS: Record<string, string> = {
  customer_held_wall: 'Zeď držel zákazník, ne dealer',
  vol_regime_shift: 'Skok volatility přeskládal mapu',
  non_hedging_actor: 'Blok od nehedgujícího aktéra',
  level_as_target: 'Bral jsem úroveň jako cílovku',
  map_moved: 'Mapa se během obchodu posunula',
}

export const FAILURE_MODES = Object.keys(FAILURE_MODE_LABELS)

export interface JournalMeta {
  types: JournalType[]
  profiles: JournalProfile[]
  grades: JournalGrade[]
  directions: TradeDirection[]
  mistake_tags: string[]
  symbols: string[]
}

const EMPTY_META: JournalMeta = {
  types: ['pozorovani', 'hypoteza', 'retro_dne', 'obchod'],
  profiles: ['smb', 'futures'],
  grades: ['A', 'B', 'C'],
  directions: ['long', 'short'],
  mistake_tags: [],
  symbols: [],
}

/** Popisky chyb; server drží jen klíče, ať se výčet nerozejde. */
export const MISTAKE_LABELS: Record<string, string> = {
  chased_entry: 'Naháněný vstup',
  moved_stop: 'Posunutý stop',
  oversized: 'Příliš velká pozice',
  undersized: 'Příliš malá pozice',
  revenge_trade: 'Revenge trade',
  fomo: 'FOMO',
  early_exit: 'Předčasný výstup',
  late_exit: 'Pozdní výstup',
  no_plan: 'Bez plánu',
  off_plan: 'Mimo plán',
  overtrading: 'Overtrading',
}

export function mistakeLabel(tag: string): string {
  return MISTAKE_LABELS[tag] ?? tag
}

/**
 * Plánované R:R z uložených polí. Odvozeniny se záměrně neukládají —
 * druhá kopie by se při editaci rozešla s pravdou.
 */
export function plannedRR(trade: JournalTrade): number | null {
  const { planned_entry: entry, planned_stop: stop, planned_target: target } = trade
  if (entry === null || stop === null || target === null) return null
  const risk = Math.abs(entry - stop)
  if (risk === 0) return null
  return Math.abs(target - entry) / risk
}

/** Realizované R — kolik násobků rizika obchod skutečně přinesl. */
export function realizedR(trade: JournalTrade): number | null {
  const { actual_entry: entry, actual_exit: exit, planned_stop: stop } = trade
  if (entry === null || exit === null || stop === null) return null
  const risk = Math.abs(entry - stop)
  if (risk === 0) return null
  const move = trade.direction === 'long' ? exit - entry : entry - exit
  return move / risk
}

export async function fetchJournal(filters: {
  symbol?: string
  date?: string
  entryType?: JournalType
  profile?: JournalProfile
  query?: string
  limit?: number
}): Promise<JournalEntry[]> {
  const params = new URLSearchParams()
  if (filters.symbol) params.set('symbol', filters.symbol)
  if (filters.date) params.set('date', filters.date)
  if (filters.entryType) params.set('entry_type', filters.entryType)
  if (filters.profile) params.set('profile', filters.profile)
  if (filters.query) params.set('q', filters.query)
  if (filters.limit) params.set('limit', String(filters.limit))
  try {
    const response = await fetch(`${API_BASE}/journal?${params}`)
    if (!response.ok) return []
    return ((await response.json()) as { journal?: JournalEntry[] }).journal ?? []
  } catch {
    return []
  }
}

/** Karta setupu v PlayBooku (#710). */
export interface PlaybookItem {
  id: number
  key: string
  name: string
  profile: 'smb' | 'futures' | 'both'
  thesis: string
  entry_conditions: string
  invalidation: string
  management: string
  active: boolean
  created_ts: string
  updated_ts: string | null
}

export async function fetchPlaybook(includeInactive = false): Promise<PlaybookItem[]> {
  const params = includeInactive ? '?include_inactive=true' : ''
  try {
    const response = await fetch(`${API_BASE}/playbook${params}`)
    if (!response.ok) return []
    return ((await response.json()) as { playbook?: PlaybookItem[] }).playbook ?? []
  } catch {
    return []
  }
}

export async function createPlaybookItem(item: {
  key: string
  name: string
  profile?: string
  thesis?: string
  entry_conditions?: string
  invalidation?: string
  management?: string
}): Promise<PlaybookItem | null> {
  try {
    const response = await fetch(`${API_BASE}/playbook`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(item),
    })
    return response.ok ? ((await response.json()) as PlaybookItem) : null
  } catch {
    return null
  }
}

export async function updatePlaybookItem(
  id: number,
  patch: Partial<Omit<PlaybookItem, 'id' | 'key' | 'created_ts' | 'updated_ts'>>,
): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/playbook/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    })
    return response.ok
  } catch {
    return false
  }
}

export async function fetchJournalMeta(): Promise<JournalMeta> {
  try {
    const response = await fetch(`${API_BASE}/journal/meta`)
    if (!response.ok) return EMPTY_META
    return (await response.json()) as JournalMeta
  } catch {
    return EMPTY_META
  }
}

export async function createJournalEntry(entry: {
  ts_ref: string
  symbol: string
  entry_type: JournalType
  text: string
  tags: string[]
  profile?: JournalProfile
  setup_id?: number | null
  news_event_id?: number | null
  trade?: Partial<JournalTrade> & { direction: TradeDirection }
  context?: Record<string, unknown> | null
  daily?: Record<string, unknown> | null
  missed_reason?: string | null
}): Promise<JournalEntry | null> {
  try {
    const response = await fetch(`${API_BASE}/journal`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(entry),
    })
    return response.ok ? ((await response.json()) as JournalEntry) : null
  } catch {
    return null
  }
}

export async function updateJournalEntry(
  id: number,
  patch: {
    text?: string
    tags?: string[]
    profile?: JournalProfile
    trade?: Partial<JournalTrade> & { direction: TradeDirection }
    daily?: Record<string, unknown> | null
  },
): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/journal/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    })
    return response.ok
  } catch {
    return false
  }
}

export async function deleteJournalEntry(id: number): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/journal/${id}`, { method: 'DELETE' })
    return response.ok
  } catch {
    return false
  }
}

/** Export záznamů do Markdownu — retrospektiva čitelná i mimo aplikaci. */
export function journalToMarkdown(entries: JournalEntry[]): string {
  const byDay = new Map<string, JournalEntry[]>()
  for (const entry of entries) {
    const day = entry.ts_ref.slice(0, 10)
    byDay.set(day, [...(byDay.get(day) ?? []), entry])
  }
  const parts: string[] = ['# Deník tradera\n']
  for (const [day, dayEntries] of [...byDay.entries()].sort()) {
    parts.push(`\n## ${day}\n`)
    for (const entry of dayEntries.sort((a, b) => a.ts_ref.localeCompare(b.ts_ref))) {
      const time = entry.ts_ref.slice(11, 16)
      const tags = entry.tags.length > 0 ? ` — ${entry.tags.map((t) => `#${t}`).join(' ')}` : ''
      parts.push(
        `**${time} · ${entry.symbol} · ${JOURNAL_TYPE_LABELS[entry.entry_type]}**${tags}\n\n${entry.text}\n`,
      )
      // Strukturovaná pole se exportují taky — retrospektiva má být čitelná
      // i mimo aplikaci (původní požadavek #673)
      const detail = entryDetailLines(entry)
      if (detail.length > 0) parts.push(`${detail.join('\n')}\n`)
    }
  }
  return parts.join('\n')
}

/** Řádky strukturovaného detailu pro export — obchod, důvod, kontext. */
function entryDetailLines(entry: JournalEntry): string[] {
  const lines: string[] = []
  const { trade } = entry
  if (trade) {
    const bits: string[] = [trade.direction === 'long' ? 'Long' : 'Short']
    if (trade.setup_key) bits.push(`setup ${trade.setup_key}`)
    if (trade.actual_entry !== null) bits.push(`vstup ${trade.actual_entry}`)
    if (trade.actual_exit !== null) bits.push(`výstup ${trade.actual_exit}`)
    const r = realizedR(trade)
    if (r !== null) bits.push(`${r >= 0 ? '+' : ''}${r.toFixed(2)}R`)
    if (trade.setup_grade) bits.push(`kvalita ${trade.setup_grade}`)
    if (trade.execution_grade) bits.push(`exekuce ${trade.execution_grade}`)
    if (trade.failure_mode) bits.push(FAILURE_MODE_LABELS[trade.failure_mode] ?? trade.failure_mode)
    for (const tag of trade.mistake_tags) bits.push(mistakeLabel(tag))
    lines.push(`- Obchod: ${bits.join(' · ')}`)
  }
  if (entry.missed_reason) {
    lines.push(`- Nevzal jsem: ${MISSED_REASON_LABELS[entry.missed_reason] ?? entry.missed_reason}`)
  }
  const context = entry.context
  if (context) {
    const bits: string[] = []
    for (const key of ['regime', 'flip', 'spot', 'session_segment', 'vol_bucket', 'macro_event']) {
      const value = context[key]
      if (value !== null && value !== undefined && value !== '') {
        bits.push(`${key} ${String(value)}`)
      }
    }
    if (bits.length > 0) lines.push(`- Kontext: ${bits.join(' · ')}`)
  }
  return lines
}
