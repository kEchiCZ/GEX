/** Chip IV percentilu v hlavičce (#871) — vpravo vedle chipu sentimentu
(přesun na žádost uživatele 27. 8.).

Primárně percentil řady `ibkr` (rozhodnutí uživatele 26. 8.: robustní a
konzistentní s vol režimem ADR-0028); IV Rank a tasty křížová kontrola žijí
v tooltipu. Bez dat se chip NEkreslí — hlavička nemá ukazovat prázdné pole
ani dosazený „normál". Denní hodnota → obnova à 10 min stačí.
*/
import { useEffect, useState } from 'react'
import { fetchIvRankLatest, ivRankPrimary, ivRankTooltip } from '../api/briefing'
import type { IvRankRow } from '../api/briefing'
import { useAppState } from '../state/AppState'

const REFRESH_MS = 10 * 60_000

export function IvRankChip() {
  const { symbol } = useAppState()
  const [rows, setRows] = useState<IvRankRow[]>([])

  useEffect(() => {
    let cancelled = false
    const load = () => {
      void fetchIvRankLatest(symbol).then((result) => {
        if (!cancelled) setRows(result)
      })
    }
    load()
    const timer = window.setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [symbol])

  const primary = ivRankPrimary(rows)
  if (primary === null) return null
  return (
    <span className="chip iv-rank-chip" data-testid="ivrank-chip" title={ivRankTooltip(rows)}>
      IV p{Math.round((primary.iv_percentile ?? 0) * 100)}
    </span>
  )
}
