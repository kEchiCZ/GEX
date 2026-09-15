/** Forward GEX bloky (#572): fetch + obnova à 10 min, jen když jsou vidět.

Pole se v enginu přepočítává po ranním OI archivu (1× při změně OI), takže
minutová kadence nemá smysl; 10 min drží čerstvost přes ranní obnovy snímku.
*/
import { useEffect, useState } from 'react'
import { fetchGexForward } from '../api/forward'
import type { ForwardBlock } from '../heatmap/dailyforward'

const REFRESH_MS = 10 * 60_000

const EMPTY: ForwardBlock[] = []

export function useGexForward(symbol: string, enabled: boolean): ForwardBlock[] {
  // Výsledek nese symbol, pro který platí: při přepnutí symbolu se vrací
  // prázdno odvozením, ne resetem stavu v efektu (#1123)
  const [loaded, setLoaded] = useState<{ symbol: string; blocks: ForwardBlock[] } | null>(null)

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    const load = () => {
      void fetchGexForward(symbol).then((result) => {
        if (!cancelled) setLoaded({ symbol, blocks: result })
      })
    }
    load()
    const timer = window.setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [symbol, enabled])

  return enabled && loaded?.symbol === symbol ? loaded.blocks : EMPTY
}
