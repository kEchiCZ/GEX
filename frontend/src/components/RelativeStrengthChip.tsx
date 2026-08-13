/** Chip relativní síly ES vs. NQ (#680, Traders mode) — widget na zkoušku.

Fetch lehkých barů obou symbolů à 60 s (GET /bars, #674) a normalizovaný
spread od US openu. Kladný spread = ES silnější. Kdyžtak se odstraní bez
následků — nic na něm nestaví (režim na zkoušku jako settle watch #603).
*/
import { useEffect, useState } from 'react'
import { fetchBars, usOpenMs } from '../api/briefing'
import { formatPct, relativeStrength } from '../instrument/relativestrength'
import type { RelativeStrength } from '../instrument/relativestrength'
import { sessionDateIso } from '../instrument/tz'
import { useAppState } from '../state/AppState'

const REFRESH_MS = 60_000
const PAIR = ['ES', 'NQ'] as const

export function RelativeStrengthChip() {
  const { tradersMode } = useAppState()
  const [rs, setRs] = useState<RelativeStrength | null>(null)

  useEffect(() => {
    if (!tradersMode) {
      setRs(null)
      return
    }
    let cancelled = false
    const load = () => {
      const dateIso = sessionDateIso()
      void Promise.all(PAIR.map((sym) => fetchBars(sym, dateIso))).then(([es, nq]) => {
        if (!cancelled) setRs(relativeStrength(es, nq, usOpenMs(dateIso)))
      })
    }
    load()
    const timer = window.setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [tradersMode])

  if (!tradersMode || rs === null) return null
  const leader = rs.spreadPb >= 0 ? 'ES' : 'NQ'
  return (
    <span
      className="muted rs-chip"
      data-testid="rs-chip"
      title={
        `Relativní síla od ${rs.fromOpen ? 'US openu' : 'začátku seance (před US openem)'}: ` +
        `ES ${formatPct(rs.pctA)} %, NQ ${formatPct(rs.pctB)} %. ` +
        `Kladný spread = ES silnější.`
      }
    >
      RS {leader} vede · ES−NQ {formatPct(rs.spreadPb)} pb
    </span>
  )
}
