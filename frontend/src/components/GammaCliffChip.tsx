/** Chip „dnes odpadá X % gammy" (#576) — informace před koncem seance.

Živý podíl gammy settlující expirace z GET /gammacliff/{symbol}; refresh
à 5 min (OI se intradenně skoro nemění, kadence stačí). Fáze 1 jen měří —
chip nic nezapíná, jen říká, jak velký útes dnes přijde.
*/
import { useEffect, useState } from 'react'
import { API_BASE } from '../config'

const REFRESH_MS = 5 * 60_000

interface CliffToday {
  cliff_share: number | null
  is_opex: boolean
}

export function formatCliffShare(share: number | null): string | null {
  if (share === null || !Number.isFinite(share)) return null
  return `${Math.round(share * 100)} %`
}

export function GammaCliffChip({ symbol }: { symbol: string }) {
  const [today, setToday] = useState<CliffToday | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const response = await fetch(`${API_BASE}/gammacliff/${symbol}`)
        if (!response.ok) return
        const payload = (await response.json()) as { today: CliffToday | null }
        if (!cancelled) setToday(payload.today)
      } catch {
        // API nedostupné — chip prostě není; hlavička nesmí spadnout
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [symbol])

  const label = today ? formatCliffShare(today.cliff_share) : null
  if (!label) return null
  return (
    <span
      className="muted gamma-cliff-chip"
      data-testid="gamma-cliff-chip"
      title={
        'Gamma útes (#576): podíl Σ|NetGEX| dnešní expirace na všech sledovaných ' +
        'expiracích — tolik gammy po settle zmizí ze dne na den. Velký útes = struktura, ' +
        'která dnes cenu drží, zítra nemusí existovat (běžně ~15 %, před OPEX ~60 %). ' +
        'Jen měření, nic se podle toho nespíná.'
      }
    >
      · odpadá {label} gammy{today?.is_opex ? ' (OPEX)' : ''}
    </span>
  )
}
