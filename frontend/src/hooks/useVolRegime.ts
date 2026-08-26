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
  const [row, setRow] = useState<VolRegimeRow | null>(null)

  useEffect(() => {
    if (!enabled) {
      setRow(null)
      return
    }
    let cancelled = false
    const load = () => {
      void fetchVolRegimeLatest(symbol).then((result) => {
        if (!cancelled) setRow(result)
      })
    }
    load()
    const timer = window.setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [symbol, enabled])

  return row
}
