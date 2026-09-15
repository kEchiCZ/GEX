/** Poslední vol režim instrumentu (ADR-0028) pro kalkulačku pozice (#874).

Denní hodnota (počítá se po settle) — obnova à 10 min bohatě stačí a drží
čerstvost přes večerní přepočet. Null = málo vzorků / engine nepočítal;
konzumenti pak nic nezobrazují (žádný default).
*/
import { useEffect, useState } from 'react'
import { fetchVolRegimeLatest } from '../api/briefing'
import type { VolRegimeRow } from '../api/briefing'

const REFRESH_MS = 10 * 60_000

export function useVolRegime(symbol: string, enabled: boolean): VolRegimeRow | null {
  // Hodnota nese symbol, pro který platí: cizí symbol se odfiltruje při
  // renderu, ne resetem stavu v efektu (#1123)
  const [loaded, setLoaded] = useState<{ symbol: string; row: VolRegimeRow | null } | null>(null)

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    const load = () => {
      void fetchVolRegimeLatest(symbol).then((result) => {
        if (!cancelled) setLoaded({ symbol, row: result })
      })
    }
    load()
    const timer = window.setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [symbol, enabled])

  return enabled && loaded?.symbol === symbol ? loaded.row : null
}
