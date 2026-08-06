/** Kalibrovaná FA α per symbol (#232 fáze 2) — zdroj pro badge ve stavové liště.

α se mění nejvýš jednou denně (ranní kalibrace po OI archivu), takže stačí
načtení při startu a obnova à 15 minut. Bez dat (API neběží, kalibrace ještě
nemá první bod) vrací null a badge se neukáže — engine tou dobou jede na
defaultu z konfigurace.
*/
import { useEffect, useState } from 'react'
import { API_BASE } from '../config'

export interface FaAlphaState {
  alpha: number
  days: number
}

const REFRESH_MS = 15 * 60_000

interface AlphaRow {
  symbol: string
  alpha: number
  days: number
}

export function useFaAlpha(symbol: string): FaAlphaState | null {
  const [bySymbol, setBySymbol] = useState<Record<string, FaAlphaState>>({})

  useEffect(() => {
    let cancelled = false
    const load = () => {
      fetch(`${API_BASE}/fa/alpha`)
        .then((response) => (response.ok ? response.json() : null))
        .then((payload: { alphas?: AlphaRow[] } | null) => {
          if (cancelled || !payload?.alphas) return
          const next: Record<string, FaAlphaState> = {}
          for (const row of payload.alphas) {
            if (typeof row.symbol === 'string' && Number.isFinite(row.alpha)) {
              next[row.symbol] = { alpha: row.alpha, days: Number(row.days) || 0 }
            }
          }
          setBySymbol(next)
        })
        .catch(() => {
          // API neběží — badge se prostě neukáže
        })
    }
    load()
    const id = setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  return bySymbol[symbol] ?? null
}

/** České skloňování dnů pro badge: 1 den, 2–4 dny, jinak dní. */
export function daysLabel(days: number): string {
  if (days === 1) return '1 den'
  if (days >= 2 && days <= 4) return `${days} dny`
  return `${days} dní`
}
