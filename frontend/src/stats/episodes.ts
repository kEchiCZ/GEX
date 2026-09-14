/** Statistika korekčních epizod SentIndexu pro Stats (#565, ADR-0037) — čisté funkce.

Epizoda = pokles denního close_z pod 20denní maximum o ≥ D σ; pokus = zahlazeno
(zpět nad maximum) do H obchodních dní, negace = ne. Řádky nese `/stats/episodes`
(plní WavesJob), parametry D/H nese `params_version` — verze se nemíchají.
*/
import { EPISODE_PRELIMINARY_MIN } from '../api/news'
import type { EpisodeRow } from '../api/news'

export interface EpisodeClassStats {
  count: number
  meanDepth: number
  meanLength: number
}

export interface EpisodeStats {
  attempts: EpisodeClassStats
  negations: EpisodeClassStats
  /** Probíhající (bez rozhodnutí). */
  open: number
  resolved: number
  /** Pod EPISODE_PRELIMINARY_MIN rozhodnutých epizod je klasifikace předběžná. */
  preliminary: boolean
  /** Verze parametrů řádků; null bez řádků, -1 při smíchaných verzích (chyba dat). */
  paramsVersion: number | null
}

function classStats(rows: EpisodeRow[]): EpisodeClassStats {
  const count = rows.length
  if (count === 0) return { count: 0, meanDepth: 0, meanLength: 0 }
  return {
    count,
    meanDepth: rows.reduce((sum, row) => sum + row.depth_z, 0) / count,
    meanLength: rows.reduce((sum, row) => sum + row.length_days, 0) / count,
  }
}

export function episodeStats(rows: EpisodeRow[]): EpisodeStats {
  const attempts = rows.filter((row) => row.label === 'attempt')
  const negations = rows.filter((row) => row.label === 'negation')
  const open = rows.filter((row) => row.label === null).length
  const resolved = attempts.length + negations.length
  const versions = new Set(rows.map((row) => row.params_version))
  return {
    attempts: classStats(attempts),
    negations: classStats(negations),
    open,
    resolved,
    preliminary: resolved < EPISODE_PRELIMINARY_MIN,
    paramsVersion: versions.size === 0 ? null : versions.size === 1 ? [...versions][0] : -1,
  }
}

/** Posledních `limit` epizod, nejnovější první. */
export function recentEpisodes(rows: EpisodeRow[], limit: number): EpisodeRow[] {
  return [...rows].sort((a, b) => (a.start_date < b.start_date ? 1 : -1)).slice(0, limit)
}

export function episodeLabelText(label: EpisodeRow['label']): string {
  return label === 'attempt' ? 'pokus' : label === 'negation' ? 'negace' : 'probíhá'
}
