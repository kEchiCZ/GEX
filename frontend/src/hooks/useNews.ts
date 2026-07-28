/** Data SentimentLensu pro UI: feed, index a topicy (#288, #289).

Jeden hook pro všechny tři konzumenty (panel, sidebar, chip v hlavičce) —
tři nezávislé fetche téhož by API zbytečně bušily a rozešly se v čase.
*/
import { useCallback, useEffect, useState } from 'react'
import { fetchNews, fetchSentimentSeries, fetchTopics, fetchUpcoming } from '../api/news'
import type { NewsRow, SentimentPoint, TopicRow } from '../api/news'
import { useAppState } from '../state/AppState'

/** Perioda přenačtení. Index se počítá po minutě, takže častěji nemá smysl. */
export const NEWS_REFRESH_MS = 60_000

/** Strop feedu v paměti — WS push jinak roste přes den bez omezení. */
const FEED_LIMIT = 200

/** Zařadí zprávu z WS do feedu (#335).

Tentýž event dorazí dvakrát: nejdřív syrový z enginu (bez kategorie, do sekund
po headline) a pak klasifikovaný z news-engine. Druhý ten první **nahrazuje**
podle `id`, jinak by feed ukazoval každou zprávu dvakrát.

Řadí se podle `ts_event` sestupně — pořadí příchodu není pořadí vzniku,
u dohnané noční fronty by se jinak staré zprávy vecpaly nahoru. */
export function mergeNewsRow(feed: NewsRow[], incoming: NewsRow): NewsRow[] {
  const without = feed.filter((row) => row.id !== incoming.id)
  return [incoming, ...without]
    .sort((a, b) => new Date(b.ts_event).getTime() - new Date(a.ts_event).getTime())
    .slice(0, FEED_LIMIT)
}

export interface NewsData {
  news: NewsRow[]
  upcoming: NewsRow[]
  series: SentimentPoint[]
  topics: TopicRow[]
  refresh: () => void
}

export function useNews(): NewsData {
  const { symbol, socket } = useAppState()
  const [news, setNews] = useState<NewsRow[]>([])
  const [upcoming, setUpcoming] = useState<NewsRow[]>([])
  const [series, setSeries] = useState<SentimentPoint[]>([])
  const [topics, setTopics] = useState<TopicRow[]>([])
  const [version, setVersion] = useState(0)

  useEffect(() => {
    let cancelled = false
    const load = () => {
      void Promise.all([
        fetchNews(),
        fetchUpcoming(),
        fetchSentimentSeries(symbol),
        fetchTopics(),
      ]).then(([feed, next, points, topicRows]) => {
        if (cancelled) return
        setNews(feed)
        setUpcoming(next)
        setSeries(points)
        setTopics(topicRows)
      })
    }
    load()
    const timer = window.setInterval(load, NEWS_REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [symbol, version])

  // Živý push (#335). Bez něj se nová zpráva objeví až s dalším REST fetchem,
  // takže headline → obrazovka trvalo minuty; teď jde o sekundy.
  useEffect(() => {
    const handler = (data: Record<string, unknown>) => {
      // Kanál `news` nese i provozní hlášky (retro pass) — ty nemají `id`
      if (typeof data.id !== 'number') return
      setNews((previous) => mergeNewsRow(previous, data as unknown as NewsRow))
    }
    socket.subscribe('news', handler)
    return () => socket.unsubscribe('news', handler)
  }, [socket])

  const refresh = useCallback(() => setVersion((previous) => previous + 1), [])
  return { news, upcoming, series, topics, refresh }
}
