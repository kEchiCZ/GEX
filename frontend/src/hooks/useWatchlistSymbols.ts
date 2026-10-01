/** Symboly watchlistu (#794 portfolio Výkonu, #1323 Knihovna setupů).

Přenačítá se periodicky (watchlist se mění v sidebaru). Nedostupný watchlist
vrací poslední známý seznam (na začátku prázdný) — volající přidá aktivní
symbol, ať obrazovka neukazuje prázdno kvůli vedlejší chybě. */
import { useEffect, useState } from 'react'
import { API_BASE } from '../config'

export function useWatchlistSymbols(pollMs: number): string[] {
  const [symbols, setSymbols] = useState<string[]>([])
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const response = await fetch(`${API_BASE}/watchlist`)
        if (!response.ok) return
        const payload = (await response.json()) as { watchlist?: { symbol: string }[] }
        if (!cancelled) setSymbols((payload.watchlist ?? []).map((item) => item.symbol))
      } catch {
        // watchlist nedostupný — drží se poslední známý seznam (viz docstring)
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), pollMs)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [pollMs])
  return symbols
}
