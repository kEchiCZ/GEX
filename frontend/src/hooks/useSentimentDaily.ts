/** Denní OHLC svíčky SentIndexu pro Daily pohled panelu (#296, SPEC 7.1). */
import { useEffect, useState } from 'react'
import { fetchSentimentDaily } from '../api/news'
import type { SentimentDailyRow } from '../api/news'

/** Denní data se mění pomalu; dnešní svíčka se dopisuje průběžně → 5 min. */
const REFRESH_MS = 300_000
/** Daily graf ukazuje jen dny v retenci snapshotů (14 dní) — rezerva navíc. */
const LOOKBACK_DAYS = 45

export function useSentimentDaily(symbol: string, enabled: boolean): SentimentDailyRow[] {
  const [rows, setRows] = useState<SentimentDailyRow[]>([])

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    const load = () => {
      const from = new Date(Date.now() - LOOKBACK_DAYS * 86_400_000).toISOString().slice(0, 10)
      void fetchSentimentDaily(symbol, from).then((daily) => {
        if (!cancelled) setRows(daily)
      })
    }
    load()
    const timer = window.setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [symbol, enabled])

  return rows
}
