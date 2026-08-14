/** Obrazovka News (#289, SPEC 9.3): feed zpráv a nadcházející plánované eventy.

Nahoře sekce Upcoming s countdownem — trader potřebuje vědět, že ve 14:30
přijde CPI, **dřív než přijde**. Pod ní filtrovatelný feed.
*/
import { useEffect, useMemo, useState } from 'react'
import {
  CATEGORY_LABELS,
  categoryGlyph,
  categoryLabel,
  countdownLabel,
  fetchCrowd,
  fetchReview,
  latestCrowd,
  primaryReaction,
  relativeAge,
  submitReview,
} from '../api/news'
import type { CrowdRow, NewsRow, ReviewRow } from '../api/news'
import { useNews } from '../hooks/useNews'
import { useSentimentState } from '../hooks/useSentimentState'

/** Crowd data se mění pomalu (F&G à 1 h, PCR à 5 min) — refresh stačí volný. */
const CROWD_REFRESH_MS = 300_000

/** Důvody zařazení do review fronty (#293) — lidsky. */
const REVIEW_REASONS: Record<string, string> = {
  disagreement: 'LLM × empirický model se rozchází',
  low_confidence: 'nízká jistota klasifikace',
}

const KIND_LABELS: Record<string, string> = {
  scheduled: 'Plánovaná',
  headline: 'Headline',
  social: 'Sociální',
  broker: 'Broker',
}

/** Číselná hodnota z API; `Numeric` sloupce můžou dorazit jako řetězec. */
function asNumber(value: number | string | null): number | null {
  if (value === null || value === '') return null
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

/** Barva badge dle skóre — stejný jazyk jako panel: teal +, červená −. */
function scoreClass(score: number | null): string {
  if (score === null || score === 0) return 'news-score neutral'
  return score > 0 ? 'news-score positive' : 'news-score negative'
}

/** Naměřený dopad na kartě (#656): barva dle znaménka, tooltip všechna okna. */
function ImpactValue({ row }: { row: NewsRow }) {
  const primary = primaryReaction(row)
  if (!primary) return null
  const windows = Object.entries(row.reactions_bp ?? {})
    .map(([minutes, value]) => `+${minutes} min: ${Number(value).toFixed(1)} bp`)
    .join(' · ')
  const title =
    `Naměřený pohyb trhu v párovacím okně po zprávě (${windows}).` +
    (row.reaction_contaminated
      ? ' ⚠ Do okna spadl jiný významný event — pohyb nejde přičíst jen téhle zprávě.'
      : '')
  return (
    <span className={scoreClass(primary.bp)} title={title} data-testid={`impact-${row.id}`}>
      Δ{primary.windowMin}m {primary.bp >= 0 ? '+' : ''}
      {primary.bp.toFixed(1)} bp{row.reaction_contaminated ? ' ⚠' : ''}
    </span>
  )
}

function NewsRowItem({
  row,
  review,
  onCorrect,
}: {
  row: NewsRow
  review: ReviewRow | undefined
  onCorrect: (eventId: number, correction: { direction?: number; category?: string }) => void
}) {
  const score = asNumber(row.sentiment_score)
  const [editing, setEditing] = useState(false)
  const [direction, setDirection] = useState<number>(row.sentiment_dir ?? 0)
  const [category, setCategory] = useState<string>(row.category ?? 'OTHER')
  return (
    <article
      data-testid={`news-row-${row.id}`}
      className={review ? 'news-card news-review-flag' : 'news-card'}
      title={review ? `Ke kontrole: ${REVIEW_REASONS[review.reason] ?? review.reason}` : undefined}
    >
      <div className="news-card-head">
        <span className="news-category">
          <span title={categoryLabel(row.category)}>{categoryGlyph(row.category)}</span>{' '}
          {categoryLabel(row.category)}
        </span>
        <span className="news-time muted" title={new Date(row.ts_event).toLocaleString()}>
          {relativeAge(row.ts_event, Date.now())}
        </span>
        <span className="muted">{KIND_LABELS[row.kind] ?? row.kind}</span>
        <span className="muted" title={`Důležitost ${row.importance ?? 1}/3`}>
          {'!'.repeat(Math.min(3, Math.max(1, row.importance ?? 1)))}
        </span>
        <span className={scoreClass(score)} title="Skóre klasifikace (směr × váha)">
          {score === null ? '—' : score.toFixed(2)}
        </span>
        {/* Naměřený dopad (#656) — co trh PO zprávě skutečně udělal */}
        <ImpactValue row={row} />
      </div>
      <p className="news-card-title">
        {review && (
          <button
            type="button"
            className="chip news-review-badge"
            aria-label={`Zkontrolovat klasifikaci: ${row.title}`}
            title={REVIEW_REASONS[review.reason] ?? review.reason}
            onClick={() => setEditing((value) => !value)}
          >
            ⚠
          </button>
        )}{' '}
        {row.title}
      </p>
      {row.summary && <p className="muted news-card-summary">{row.summary}</p>}
      {editing && review && (
        <span className="news-review-edit">
          <select
            aria-label="Oprava směru"
            value={direction}
            onChange={(event) => setDirection(Number(event.target.value))}
          >
            <option value={1}>+1 risk-on</option>
            <option value={0}>0 neutrální</option>
            <option value={-1}>−1 risk-off</option>
          </select>
          <select
            aria-label="Oprava kategorie"
            value={category}
            onChange={(event) => setCategory(event.target.value)}
          >
            {Object.keys(CATEGORY_LABELS).map((key) => (
              <option key={key} value={key}>
                {categoryLabel(key)}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="chip"
            onClick={() => {
              onCorrect(row.id, { direction, category })
              setEditing(false)
            }}
          >
            Opravit
          </button>
        </span>
      )}
    </article>
  )
}

/** Crowd blok (#290, SPEC 5.8): doplňkový pohled MIMO SentIndex. */
function CrowdBlock({ rows }: { rows: CrowdRow[] }) {
  const latest = latestCrowd(rows)
  const fearGreed = latest.get('cnn_fg|score|')
  const rating =
    fearGreed?.raw && typeof fearGreed.raw.rating === 'string' ? fearGreed.raw.rating : null
  const pcrs = ['ES', 'NQ']
    .map((symbol) => ({ symbol, row: latest.get(`gexlens|pcr_volume|${symbol}`) }))
    .filter((entry) => entry.row !== undefined)
  const reddit = [...latest.values()].filter((row) => row.source === 'reddit')
  if (!fearGreed && pcrs.length === 0 && reddit.length === 0) return null
  return (
    <div className="news-topics" aria-label="Crowd sentiment">
      <span className="muted">Crowd (mimo index):</span>
      {fearGreed && (
        <span title="CNN Fear & Greed">
          F&G {asNumber(fearGreed.value)?.toFixed(0) ?? '—'}
          {rating ? ` (${rating})` : ''}
        </span>
      )}
      {pcrs.map(({ symbol, row }) => (
        <span key={symbol} title={`Put/call volume ratio ${symbol} (vlastní opční data)`}>
          PCR {symbol} {asNumber(row!.value)?.toFixed(2) ?? '—'}
        </span>
      ))}
      {reddit.map((row) => (
        <span key={row.metric} title="Průměrné skóre hot postů">
          r/{row.metric.startsWith('wsb') ? 'wallstreetbets' : 'stocks'}{' '}
          {asNumber(row.value)?.toFixed(0) ?? '—'}
        </span>
      ))}
    </div>
  )
}

export function NewsView() {
  const { news, upcoming, topics } = useNews()
  // Aktuální stav + trend (#656 bod 6) — týž zdroj jako chip v hlavičce
  const sentState = useSentimentState()
  const [category, setCategory] = useState<string>('')
  const [minImportance, setMinImportance] = useState<number>(0)
  const [crowd, setCrowd] = useState<CrowdRow[]>([])
  const [review, setReview] = useState<ReviewRow[]>([])

  useEffect(() => {
    let cancelled = false
    const load = () => {
      void fetchCrowd().then((rows) => {
        if (!cancelled) setCrowd(rows)
      })
      void fetchReview().then((rows) => {
        if (!cancelled) setReview(rows)
      })
    }
    load()
    const timer = setInterval(load, CROWD_REFRESH_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  const reviewByEvent = useMemo(
    () => new Map(review.map((item) => [item.event_id, item])),
    [review],
  )
  const handleCorrect = (
    eventId: number,
    correction: { direction?: number; category?: string },
  ) => {
    void submitReview(eventId, correction).then((ok) => {
      // Oprava = nová verze klasifikace; refetch stáhne aktualizovaný feed
      if (ok) void fetchReview().then(setReview)
    })
  }

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

      {sentState && (
        <div className="news-topics" aria-label="Stav sentimentu">
          <span className="muted">Stav:</span>
          <span className={`news-state state-${sentState.state.toLowerCase()}`}>
            {sentState.state}
            {sentState.unconfirmed ? ' (nepotvrzený)' : ''}
          </span>
          <span className="muted">
            trend {sentState.polarity === 'up' ? '↑' : sentState.polarity === 'down' ? '↓' : '→'}
          </span>
        </div>
      )}

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

      <CrowdBlock rows={crowd} />

      {upcoming.length > 0 && (
        <div className="news-upcoming" aria-label="Nadcházející události">
          <h3>Nadcházející</h3>
          <ul>
            {upcoming.slice(0, 8).map((row) => (
              <li key={row.id} data-testid={`upcoming-${row.id}`}>
                <strong>{row.title}</strong>
                <span className="muted"> {countdownLabel(row.ts_event)}</span>
                {asNumber(row.forecast) !== null && (
                  <span className="muted">
                    {' '}
                    · konsensus {asNumber(row.forecast)}
                    {asNumber(row.previous) !== null ? ` (min. ${asNumber(row.previous)})` : ''}
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
        <div className="news-cards" aria-label="Feed zpráv">
          {filtered.map((row) => (
            <NewsRowItem
              key={row.id}
              row={row}
              review={reviewByEvent.get(row.id)}
              onCorrect={handleCorrect}
            />
          ))}
        </div>
      )}
    </section>
  )
}
