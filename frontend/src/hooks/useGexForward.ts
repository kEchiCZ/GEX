/** Forward GEX bloky (#572): fetch + obnova à 10 min, jen když jsou vidět.

Pole se v enginu přepočítává po ranním OI archivu (1× při změně OI), takže
minutová kadence nemá smysl; 10 min drží čerstvost přes ranní obnovy snímku.
*/
import { useEffect, useState } from 'react'
import { fetchGexForward } from '../api/forward'
import type { ForwardBlock } from '../heatmap/dailyforward'

const REFRESH_MS = 10 * 60_000

export function useGexForward(symbol: string, enabled: boolean): ForwardBlock[] {
  const [blocks, setBlocks] = useState<ForwardBlock[]>([])

  useEffect(() => {
    if (!enabled) {
      setBlocks([])
      return
    }
    let cancelled = false
    const load = () => {
      void fetchGexForward(symbol).then((result) => {
        if (!cancelled) setBlocks(result)
      })
    }
    load()
    const timer = window.setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [symbol, enabled])

  return blocks
}
