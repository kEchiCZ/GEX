/** Souhrnný indikátor tendence (#350): REST klient + popisky pásem. */
import { API_BASE } from '../config'

export interface TendencyVote {
  name: string
  vote: number
  weight: number
  detail: string
}

export interface TendencyRow {
  ts_min: string
  symbol: string
  score: number
  band: string
  votes: TendencyVote[]
  weights_version: number
}

export const BAND_LABELS: Record<string, string> = {
  strong_short: 'Strong Short',
  short: 'Short',
  neutral: 'Neutral',
  long: 'Long',
  strong_long: 'Strong Long',
}

export const BAND_ORDER = ['strong_short', 'short', 'neutral', 'long', 'strong_long'] as const

/** České názvy složek — rozpad hlasů nesmí ukazovat DB konstanty. */
export const VOTE_LABELS: Record<string, string> = {
  flip: 'Poloha vůči Gamma Flipu',
  walls_distance: 'Vzdálenost k zdem',
  walls_dominance: 'Dominance zdí',
  max_pain: 'Poloha vůči Max Painu',
  centroid: 'Poloha vůči těžišti',
  cum_delta_slope: 'Sklon Cum Δ',
  divergence: 'Rozchod ceny a Cum Δ',
  delta_flow: 'Δ Flow C/P',
  sentindex: 'SentIndex',
  gamma_at_price: 'GEX v místě ceny',
  charm_flow: 'Charm tok do close',
  vanna_flow: 'Vanna × trend IV',
}

export async function fetchTendency(symbol: string, date?: string): Promise<TendencyRow[]> {
  try {
    const query = date ? `?date=${date}` : ''
    const response = await fetch(`${API_BASE}/tendency/${symbol}${query}`)
    if (!response.ok) return []
    const data = (await response.json()) as { tendency: TendencyRow[] }
    return data.tendency ?? []
  } catch {
    return []
  }
}
