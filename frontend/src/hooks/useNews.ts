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

export interface NewsData {
  news: NewsRow[]
  upcoming: NewsRow[]
  series: SentimentPoint[]
  topics: TopicRow[]
  refresh: () => void
}

export function useNews(): NewsData {
  const { symbol } = useAppState()
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

  const refresh = useCallback(() => setVersion((previous) => previous + 1), [])
  return { news, upcoming, series, topics, refresh }
}
