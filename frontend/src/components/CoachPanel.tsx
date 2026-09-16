/** Kouč (#1187 fáze 3, #933) v Deníku: denní review obchodů s příznaky a skóre,
týdenní report s cenou chyb a 1–3 pravidly. Data z /coach/*; refresh při
změně dne, symbolu nebo verze deníku. */
import { useEffect, useState } from 'react'
import { fetchCoachReview, fetchCoachWeekly, formatR, scoreTone } from '../api/coach'
import type { CoachDaily, CoachWeekly } from '../api/coach'

function timeOf(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  return Number.isNaN(date.getTime())
    ? '—'
    : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export function CoachPanel({
  date,
  symbol,
  version = 0,
}: {
  /** Obchodní den (YYYY-MM-DD); prázdné = dnešní seance. */
  date: string
  /** Filtr symbolu; prázdné = všechny. */
  symbol: string
  /** Bump po zápisu do deníku — vynutí přenačtení. */
  version?: number
}) {
  const [daily, setDaily] = useState<CoachDaily | null>(null)
  const [weekly, setWeekly] = useState<CoachWeekly | null>(null)
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    let cancelled = false
    void Promise.all([
      fetchCoachReview(date || undefined, symbol || undefined),
      fetchCoachWeekly(date || undefined, symbol || undefined),
    ]).then(([review, report]) => {
      if (cancelled) return
      setDaily(review)
      setWeekly(report)
      setLoaded(true)
    })
    return () => {
      cancelled = true
    }
  }, [date, symbol, version])

  if (!loaded) return null
  const tone = scoreTone(daily?.score ?? null)
  return (
    <section className="coach-panel" aria-label="Kouč">
      <h3>
        🎓 Kouč
        {daily && (
          <span className={`coach-score coach-${tone}`} data-testid="coach-score">
            {daily.n > 0 ? `${daily.score}/100` : '—'}
          </span>
        )}
      </h3>
      {daily === null ? (
        <p className="muted">Kouč není dostupný (API).</p>
      ) : (
        <>
          <p className="muted" data-testid="coach-summary">
            {daily.session_day.split('-').reverse().join('. ')}: {daily.summary}
          </p>
          {daily.trades.length > 0 && (
            <ul className="coach-trades">
              {daily.trades.map((trade) => (
                <li key={trade.id} data-testid="coach-trade">
                  <span className="coach-trade-head">
                    {timeOf(trade.opened_ts)} {trade.direction.toUpperCase()} {trade.symbol}
                    {trade.setup_key ? ` · ${trade.setup_key}` : ''}
                    {trade.paper ? ' · paper' : ''} ·{' '}
                    <b className={(trade.realized_r ?? 0) >= 0 ? 'r-positive' : 'r-negative'}>
                      {formatR(trade.realized_r)}
                    </b>
                    {trade.planned_rr !== null ? ` (plán ${trade.planned_rr.toFixed(1)} R)` : ''}
                    {trade.capture !== null
                      ? ` · využito ${Math.round(trade.capture * 100)} %`
                      : ''}
                  </span>
                  {trade.flags.length === 0 ? (
                    <span className="muted"> ✓ bez příznaků</span>
                  ) : (
                    <ul className="coach-flags">
                      {trade.flags.map((flag) => (
                        <li key={flag.kind} className="coach-flag" title={flag.detail}>
                          ⚠ {flag.label}
                          {flag.cost_r !== null && flag.cost_r < 0
                            ? ` (${formatR(flag.cost_r)})`
                            : ''}{' '}
                          <span className="muted">— {flag.detail}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      {weekly && weekly.n > 0 && (
        <div className="coach-weekly" data-testid="coach-weekly">
          <h4>
            Týden {weekly.week_start.split('-').reverse().join('. ')} –{' '}
            {weekly.week_end.split('-').reverse().join('. ')}
          </h4>
          <p className="muted">
            {weekly.n} obchodů · {formatR(weekly.total_r)} · úspěšnost{' '}
            {weekly.win_rate === null ? '—' : `${Math.round(weekly.win_rate * 100)} %`} · Ø{' '}
            {formatR(weekly.avg_r)} · disciplína {weekly.score ?? '—'}
            {weekly.capture !== null
              ? ` · využití pohybu ${Math.round(weekly.capture * 100)} %`
              : ''}
          </p>
          {weekly.rules.length > 0 && (
            <ol className="coach-rules">
              {weekly.rules.map((rule) => (
                <li key={rule.kind}>
                  <b>{rule.advice}</b>{' '}
                  <span className="muted">
                    ({rule.n}× {rule.cost_r < 0 ? `, stálo ${formatR(rule.cost_r)}` : ''})
                  </span>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </section>
  )
}
