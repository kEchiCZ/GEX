/** Témata v čase (#566, princip z analýzy #561) — panel v obrazovce News.

Tři pohledy nad kategoriemi zpráv: rozpad „co trh za období řeší" (podíl
vah témat), sparkline kumulativního indexu tématu (téma se kazí postupně,
v souhrnném SentIndexu to zanikne) a po rozkliku zprávy, které téma tvoří
(hodnota musí být dohledatelná ke zdrojům). Vzhled je náš — z reference se
bere princip, ne rozvržení.
*/
import { useEffect, useMemo, useRef, useState } from 'react'
import { categoryGlyph, categoryLabel, fetchTopicEvents, fetchTopicSeries } from '../api/news'
import type { TopicEventRow, TopicSeriesRow } from '../api/news'

/** Nabídka období: den obchodníka, týden narativu, měsíc/rok kontextu. */
const RANGES = [
  { days: 1, label: 'Den' },
  { days: 7, label: 'Týden' },
  { days: 30, label: 'Měsíc' },
  { days: 365, label: 'Rok' },
] as const

const REFRESH_MS = 5 * 60_000

/** Sparkline řady tématu — SVG polyline s nulovou linkou. */
export function TopicSparkline({ points }: { points: { ts: string; value: number }[] }) {
  const W = 160
  const H = 36
  if (points.length < 2) return null
  const values = points.map((point) => point.value)
  // Symetrický rozsah kolem nuly — kladná/záporná půlka mají stejné měřítko,
  // jinak by mírně záporné téma vypadalo stejně zle jako hluboce záporné
  const span = Math.max(...values.map(Math.abs), 1e-6)
  const x = (index: number) => (index / (points.length - 1)) * W
  const y = (value: number) => H / 2 - (value / span) * (H / 2 - 2)
  const path = points.map((point, index) => `${x(index).toFixed(1)},${y(point.value).toFixed(1)}`)
  const last = values[values.length - 1]
  return (
    <svg className="topic-spark" viewBox={`0 0 ${W} ${H}`} width={W} height={H} aria-hidden="true">
      <line x1="0" y1={H / 2} x2={W} y2={H / 2} className="topic-spark-zero" />
      <polyline
        points={path.join(' ')}
        fill="none"
        className={last >= 0 ? 'topic-spark-up' : 'topic-spark-down'}
      />
    </svg>
  )
}

function eventStamp(ts: string): string {
  return new Date(ts).toLocaleString([], {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** Detail rozkliknutého tématu: zprávy, které ho v období tvoří. */
function TopicEvents({ category, days }: { category: string; days: number }) {
  const [events, setEvents] = useState<TopicEventRow[] | null>(null)
  useEffect(() => {
    let cancelled = false
    setEvents(null)
    void fetchTopicEvents(category, days).then((rows) => {
      if (!cancelled) setEvents(rows)
    })
    return () => {
      cancelled = true
    }
  }, [category, days])
  if (events === null) return <p className="muted">Načítám zprávy…</p>
  if (events.length === 0) return <p className="muted">Období nemá skórované zprávy.</p>
  return (
    <ul className="topic-events" data-testid={`topic-events-${category}`}>
      {events.map((row) => (
        <li key={row.id}>
          <span className="muted">{eventStamp(row.ts_event)}</span> {row.title}
          {row.sentiment_score !== null && (
            <span
              className={`news-score ${row.sentiment_score > 0 ? 'positive' : row.sentiment_score < 0 ? 'negative' : 'neutral'}`}
            >
              {' '}
              {row.sentiment_score >= 0 ? '+' : ''}
              {row.sentiment_score.toFixed(2)}
            </span>
          )}
        </li>
      ))}
    </ul>
  )
}

export function TopicsPanel({
  focus = null,
}: {
  /** Proklik z karty zprávy (#656 bod 2) — otevře téma a odscrolluje panel;
  objekt místo stringu, ať opakovaný klik na totéž téma efekt znovu spustí. */
  focus?: { category: string } | null
}) {
  const [days, setDays] = useState<number>(7)
  const [topics, setTopics] = useState<TopicSeriesRow[]>([])
  const [openCategory, setOpenCategory] = useState<string | null>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (focus === null) return
    setOpenCategory(focus.category)
    // jsdom scrollIntoView neumí — optional call, at testy nepadají
    panelRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
  }, [focus])

  useEffect(() => {
    let cancelled = false
    const load = () => {
      void fetchTopicSeries(days).then((rows) => {
        if (!cancelled) setTopics(rows)
      })
    }
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [days])

  const maxShare = useMemo(() => Math.max(...topics.map((topic) => topic.share), 1e-6), [topics])

  return (
    <div className="topics-panel" aria-label="Témata v čase" ref={panelRef}>
      <div className="topics-head">
        <h3>Témata</h3>
        <div className="topics-ranges" role="group" aria-label="Období">
          {RANGES.map((range) => (
            <button
              key={range.days}
              type="button"
              className={days === range.days ? 'active' : ''}
              onClick={() => setDays(range.days)}
            >
              {range.label}
            </button>
          ))}
        </div>
      </div>
      {topics.length === 0 ? (
        <p className="muted">Období nemá skórované zprávy — rozpad témat není z čeho spočítat.</p>
      ) : (
        <ol className="topics-list">
          {topics.map((topic) => (
            <li key={topic.category}>
              <button
                type="button"
                className="topic-row"
                data-testid={`topic-row-${topic.category}`}
                aria-expanded={openCategory === topic.category}
                onClick={() =>
                  setOpenCategory((prev) => (prev === topic.category ? null : topic.category))
                }
              >
                <span className="topic-name">
                  {categoryGlyph(topic.category)} {categoryLabel(topic.category)}
                </span>
                <span className="topic-share-bar" aria-hidden="true">
                  <span
                    className="topic-share-fill"
                    style={{ width: `${Math.round((topic.share / maxShare) * 100)}%` }}
                  />
                </span>
                <span className="topic-share muted">
                  {Math.round(topic.share * 100)} % · {topic.events} zpráv
                </span>
                <TopicSparkline points={topic.points} />
              </button>
              {openCategory === topic.category && (
                <TopicEvents category={topic.category} days={days} />
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}
