/** Aktuální hodnota indikátoru tendence (#350): REST snapshot + WS push. */
import { useEffect, useState } from 'react'
import { fetchTendency } from '../api/tendency'
import type { TendencyRow } from '../api/tendency'
import { useAppState } from '../state/AppState'

const REFRESH_MS = 60_000

export function useTendency(): TendencyRow | null {
  const { symbol, socket } = useAppState()
  // Hodnota nese symbol, pro který platí — přepnutí symbolu nesmí ukazovat
  // cizí hodnotu, a to odvozením při renderu, ne resetem v efektu (#1123)
  const [loaded, setLoaded] = useState<{ symbol: string; row: TendencyRow } | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = () => {
      void fetchTendency(symbol).then((rows) => {
        if (!cancelled && rows.length > 0) setLoaded({ symbol, row: rows[rows.length - 1] })
      })
    }
    load()
    const timer = window.setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [symbol])

  useEffect(() => {
    const handler = (data: Record<string, unknown>) => {
      if (typeof data.band !== 'string' || typeof data.score !== 'number') return
      setLoaded({ symbol, row: data as unknown as TendencyRow })
    }
    socket.subscribe(`tendency.${symbol}`, handler)
    return () => socket.unsubscribe(`tendency.${symbol}`, handler)
  }, [socket, symbol])

  return loaded?.symbol === symbol ? loaded.row : null
}
