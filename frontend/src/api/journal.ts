/** Deník tradera (#673, fáze A): REST klient /journal. */
import { API_BASE } from '../config'

export type JournalType = 'pozorovani' | 'hypoteza' | 'retro_dne' | 'obchod'

export const JOURNAL_TYPE_LABELS: Record<JournalType, string> = {
  pozorovani: 'Pozorování',
  hypoteza: 'Hypotéza',
  retro_dne: 'Retrospektiva dne',
  obchod: 'Obchod',
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
  created_ts: string
  updated_ts: string | null
}

export async function fetchJournal(filters: {
  symbol?: string
  date?: string
  entryType?: JournalType
}): Promise<JournalEntry[]> {
  const params = new URLSearchParams()
  if (filters.symbol) params.set('symbol', filters.symbol)
  if (filters.date) params.set('date', filters.date)
  if (filters.entryType) params.set('entry_type', filters.entryType)
  try {
    const response = await fetch(`${API_BASE}/journal?${params}`)
    if (!response.ok) return []
    return ((await response.json()) as { journal: JournalEntry[] }).journal
  } catch {
    return []
  }
}

export async function createJournalEntry(entry: {
  ts_ref: string
  symbol: string
  entry_type: Exclude<JournalType, 'obchod'>
  text: string
  tags: string[]
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
  patch: { text?: string; tags?: string[] },
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
    }
  }
  return parts.join('\n')
}
