/** Kouč (#1187 fáze 3, #933) v Deníku: denní review obchodů s příznaky a skóre,
týdenní report s cenou chyb a 1–3 pravidly. Data z /coach/*; refresh při
změně dne, symbolu nebo verze deníku. */
import { useEffect, useState } from 'react'
import {
  fetchCoachHours,
  fetchCoachReview,
  fetchCoachSummary,
  fetchCoachWeekly,
  formatR,
  rankedWindows,
  scoreTone,
} from '../api/coach'
import type {
  CoachDaily,
  CoachHours,
  CoachSummary,
  CoachTimeProfile,
  CoachWeekly,
} from '../api/coach'

/** Tabulka segmentů seance s Ø R — nejlepší/nejhorší okno zvýrazněné (#1201). */
function TimeTable({
  title,
  profile,
}: {
  title: string
  profile: CoachTimeProfile & { n: number }
}) {
  const rows = rankedWindows(profile, 1)
  if (profile.n === 0 || rows.length === 0) return null
  return (
    <div className="coach-time" data-testid="coach-time">
      <h4>
        {title} <span className="muted">({profile.n} · Praha)</span>
      </h4>
      <table className="coach-time-table">
        <tbody>
          {rows.map(({ key, label, bucket }) => {
            const tone =
              key === profile.best_segment ? 'good' : key === profile.worst_segment ? 'bad' : ''
            return (
              <tr key={key} className={tone ? `coach-${tone}` : undefined}>
                <td>{label}</td>
                <td className={bucket.avg_r >= 0 ? 'r-positive' : 'r-negative'}>
                  {formatR(bucket.avg_r)}
                </td>
                <td className="muted">
                  n={bucket.n} · {Math.round(bucket.win_rate * 100)} % · Σ {formatR(bucket.sum_r)}
                  {bucket.n < profile.min_window_sample ? ' · malý vzorek' : ''}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

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
  const [hours, setHours] = useState<CoachHours | null>(null)
  const [summary, setSummary] = useState<CoachSummary | null>(null)
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    let cancelled = false
    void Promise.all([
      fetchCoachReview(date || undefined, symbol || undefined),
      fetchCoachWeekly(date || undefined, symbol || undefined),
      fetchCoachHours(60, symbol || undefined),
      fetchCoachSummary(symbol || undefined),
    ]).then(([review, report, timeProfile, overview]) => {
      if (cancelled) return
      setDaily(review)
      setWeekly(report)
      setHours(timeProfile)
      setSummary(overview)
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
      {summary && summary.lines.length > 0 && (
        <ul className="coach-overview" data-testid="coach-overview">
          {summary.lines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
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
      {hours && (hours.trades.n > 0 || hours.setups.n > 0) && (
        <div className="coach-hours">
          <TimeTable title="Denní doba — tvoje obchody (60 dní)" profile={hours.trades} />
          <TimeTable title="Denní doba — setupy detektoru (60 dní)" profile={hours.setups} />
        </div>
      )}
    </section>
  )
}
