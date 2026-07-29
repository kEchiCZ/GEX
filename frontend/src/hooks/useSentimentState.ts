/** Stav RiskOn/RiskOff/Neutral pro chip v hlavičce a graf (#295, SPEC 9.0).

REST snapshot + živý push z kanálu `sentiment.state` (publikuje se jen při
změně stavu, takže REST à 60 s drží čerstvé i MA/threshold hodnoty).
*/
import { useEffect, useState } from 'react'
import { fetchSentimentState } from '../api/news'
import type { SentimentStateInfo } from '../api/news'
import { useAppState } from '../state/AppState'

const REFRESH_MS = 60_000

export function useSentimentState(): SentimentStateInfo | null {
  const { symbol, socket } = useAppState()
  const [state, setState] = useState<SentimentStateInfo | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = () => {
      void fetchSentimentState(symbol).then((info) => {
        // Guard tvaru: mock/degradované API může vrátit objekt bez `state`
        if (!cancelled && info && typeof info.state === 'string') setState(info)
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
      if (typeof data.state !== 'string') return
      if (typeof data.symbol === 'string' && data.symbol !== symbol) return
      setState(data as unknown as SentimentStateInfo)
    }
    socket.subscribe('sentiment.state', handler)
    return () => socket.unsubscribe('sentiment.state', handler)
  }, [socket, symbol])

  return state
}
