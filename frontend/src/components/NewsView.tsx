/** Obrazovka News (#289, SPEC 9.3): feed zpráv a nadcházející plánované eventy.

Nahoře sekce Upcoming s countdownem — trader potřebuje vědět, že ve 14:30
přijde CPI, **dřív než přijde**. Pod ní filtrovatelný feed.
*/
import { useMemo, useState } from 'react'
import { categoryGlyph, categoryLabel, countdownLabel } from '../api/news'
import type { NewsRow } from '../api/news'
import { useNews } from '../hooks/useNews'

const KIND_LABELS: Record<string, string> = {
  scheduled: 'Plánovaná',
  headline: 'Headline',
  social: 'Sociální',
  broker: 'Broker',
}

/** Barva badge dle skóre — stejný jazyk jako panel: teal +, červená −. */
function scoreClass(score: number | null): string {
  if (score === null || score === 0) return 'news-score neutral'
  return score > 0 ? 'news-score positive' : 'news-score negative'
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString('cs-CZ', { hour: '2-digit', minute: '2-digit' })
}

function NewsRowItem({ row }: { row: NewsRow }) {
  return (
    <tr data-testid={`news-row-${row.id}`}>
      <td className="news-time muted">{formatTime(row.ts_event)}</td>
      <td className="news-category">
        <span title={categoryLabel(row.category)}>{categoryGlyph(row.category)}</span>{' '}
        {categoryLabel(row.category)}
      </td>
      <td className="news-title">{row.title}</td>
      <td className="muted">{KIND_LABELS[row.kind] ?? row.kind}</td>
      <td>{row.importance ?? '—'}</td>
      <td>
        <span className={scoreClass(row.sentiment_score)}>
          {row.sentiment_score === null ? '—' : row.sentiment_score.toFixed(2)}
        </span>
      </td>
    </tr>
  )
}

export function NewsView() {
  const { news, upcoming, topics } = useNews()
  const [category, setCategory] = useState<string>('')
  const [minImportance, setMinImportance] = useState<number>(0)

  const categories = useMemo(
    () => [...new Set(news.map((row) => row.category).filter((c): c is string => !!c))].sort(),
    [news],
  )
  const filtered = useMemo(
    () =>
      news.filter(
        (row) => (!category || row.category === category) && (row.importance ?? 0) >= minImportance,
      ),
    [news, category, minImportance],
  )
  const activeTopics = useMemo(() => topics.filter((topic) => topic.active), [topics])

  return (
    <section className="news-view" aria-label="News">
      <header className="news-header">
        <h2>Zprávy</h2>
        <label>
          Kategorie{' '}
          <select value={category} onChange={(event) => setCategory(event.target.value)}>
            <option value="">vše</option>
            {categories.map((item) => (
              <option key={item} value={item}>
                {categoryLabel(item)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Min. důležitost{' '}
          <select
            value={minImportance}
            onChange={(event) => setMinImportance(Number(event.target.value))}
          >
            <option value={0}>vše</option>
            <option value={2}>2+</option>
            <option value={3}>3</option>
          </select>
        </label>
      </header>

      {activeTopics.length > 0 && (
        <div className="news-topics" aria-label="Aktivní témata">
          <span className="muted">Co hýbe trhem:</span>
          {activeTopics.map((topic) => (
            <span key={topic.category} className={scoreClass(topic.value)}>
              {categoryGlyph(topic.category)} {categoryLabel(topic.category)}{' '}
              {topic.value.toFixed(2)}
            </span>
          ))}
        </div>
      )}

      {upcoming.length > 0 && (
        <div className="news-upcoming" aria-label="Nadcházející události">
          <h3>Nadcházející</h3>
          <ul>
            {upcoming.slice(0, 8).map((row) => (
              <li key={row.id} data-testid={`upcoming-${row.id}`}>
                <strong>{row.title}</strong>
                <span className="muted"> {countdownLabel(row.ts_event)}</span>
                {row.forecast !== null && (
                  <span className="muted">
                    {' '}
                    · konsensus {row.forecast}
                    {row.previous !== null ? ` (min. ${row.previous})` : ''}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {filtered.length === 0 ? (
        <p className="muted">
          Zatím žádné zprávy — news-engine sbírá z ForexFactory, Fed RSS a zpravodajských feedů.
          Finnhub se zapne po doplnění klíče.
        </p>
      ) : (
        <table className="news-table">
          <thead>
            <tr>
              <th>Čas</th>
              <th>Kategorie</th>
              <th>Titulek</th>
              <th>Typ</th>
              <th>Důl.</th>
              <th>Skóre</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((row) => (
              <NewsRowItem key={row.id} row={row} />
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
