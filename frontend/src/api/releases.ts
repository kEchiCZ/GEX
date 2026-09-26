/** Předem registrované hypotézy o reakci na releasy (#1296, ADR-0044) — jen čtení.

Stav počítá news-engine po každém releasu; API přidává definice z registru.
Bez živého releasu je hypotéza „ověřuje se“ s n = 0 — to není chyba. */
import { API_BASE } from '../config'

export type HypothesisStatus = 'testing' | 'verified' | 'rejected'

export interface HypothesisOutcome {
  cluster_ts: string
  family: string
  hit: boolean
  value_bp: number
}

export interface HypothesisSymbolState {
  historical: { hits: number; n: number }
  hits: number
  n: number
  wilson_lb: number | null
  wilson_ub: number | null
  status: HypothesisStatus
  decided_at_n: number | null
  next_checkpoint: number | null
  outcomes: HypothesisOutcome[]
  computed_at: string | null
}

export interface ReleaseHypothesis {
  id: string
  label: string
  rule: string
  /** testing = v upozornění hned bez pravděpodobnosti, verified = až po ověření, never = ne. */
  in_preview: 'testing' | 'verified' | 'never'
  symbols: Record<string, HypothesisSymbolState>
}

export interface ReleaseHypotheses {
  registered_at: string
  criteria: { checkpoints: number[]; confidence: number; verified: string; rejected: string }
  hypotheses: ReleaseHypothesis[]
}

/** Chyba se nepolyká — sekce ji ukáže místo prázdné tabulky. */
export async function fetchReleaseHypotheses(): Promise<ReleaseHypotheses> {
  const response = await fetch(`${API_BASE}/stats/releases/hypotheses`)
  if (!response.ok) throw new Error(`stats/releases/hypotheses: HTTP ${response.status}`)
  const payload = (await response.json()) as ReleaseHypotheses
  if (!Array.isArray(payload.hypotheses)) {
    throw new Error('stats/releases/hypotheses: odpověď bez pole hypotheses')
  }
  return payload
}

/** Datum releasu / registrace česky (1. 10. 2026); nečitelný řetězec beze změny. */
export function releaseDay(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleDateString('cs-CZ', { day: 'numeric', month: 'numeric', year: 'numeric' })
}

/** Tooltip nadpisu sekce (vzor ivRankTooltip): odrážky pod sebou, ne odstavec. */
export function headingTooltip(data: ReleaseHypotheses): string {
  const points = data.criteria.checkpoints.join(', ')
  return [
    'Předem registrované hypotézy (ADR-0044)',
    '',
    `• Počítají se jen releasy od ${releaseDay(data.registered_at)}`,
    `• Rozhoduje se při n = ${points}`,
    `• Ověřeno: ${data.criteria.verified}`,
    `• Zamítnuto: ${data.criteria.rejected}`,
    '• Kritéria se po registraci nemění',
    '• Rozhodnutí je konečné — pozdější oprava dat ho nezmění',
    '• Pravděpodobnost v upozornění jen po ověření',
  ].join('\n')
}
