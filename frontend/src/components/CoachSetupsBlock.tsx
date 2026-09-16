/** Doporučení kouče nad setupy (#1201): co vypnout / na co se soustředit
(šablona × okno × režim × pásmo × důvěra, jen n ≥ min_sample a Wilson LB),
nejlepší a nejhorší okno dne, četnost příznaků. Data z /coach/setups. */
import { useEffect, useState } from 'react'
import { fetchCoachSetups, formatR } from '../api/coach'
import type { CoachSetupsReport } from '../api/coach'
import { templateLabel } from '../api/setups'

function prettyKey(scope: string, key: string): string {
  if (scope === 'template_segment' || scope === 'template_regime') {
    const [template, rest] = key.split('|')
    return `${templateLabel(template)} · ${rest}`
  }
  return key
}

export function CoachSetupsBlock({ symbol }: { symbol: string }) {
  const [report, setReport] = useState<CoachSetupsReport | null>(null)
  const [open, setOpen] = useState(true)
  useEffect(() => {
    let cancelled = false
    void fetchCoachSetups(60, symbol).then((result) => {
      if (!cancelled) setReport(result)
    })
    return () => {
      cancelled = true
    }
  }, [symbol])
  if (report === null || report.n === 0) return null
  const time = report.time
  const best = time.best_segment ? time.segments[time.best_segment] : null
  const worst = time.worst_segment ? time.segments[time.worst_segment] : null
  const flags = Object.entries(report.flags).sort((a, b) => a[1].sum_r - b[1].sum_r)
  return (
    <section className="coach-panel coach-setups" aria-label="Doporučení kouče">
      <h3>
        🎓 Kouč nad setupy{' '}
        <span className="muted">
          {report.n} uzavřených za {report.days} dní · {formatR(report.total_r)}
        </span>
        <button
          type="button"
          className="chip"
          onClick={() => setOpen((value) => !value)}
          aria-label="Doporučení kouče"
        >
          {open ? 'skrýt' : 'ukázat'}
        </button>
      </h3>
      {open && (
        <>
          {report.recommendations.length === 0 ? (
            <p className="muted">
              Zatím žádná kombinace s dostatečným vzorkem (n ≥ {report.min_sample}) a jasným
              znaménkem — sběr běží.
            </p>
          ) : (
            <ol className="coach-rules" data-testid="coach-setups-recs">
              {report.recommendations.slice(0, 6).map((rec) => (
                <li
                  key={`${rec.scope}:${rec.key}`}
                  className={rec.kind === 'avoid' ? 'r-negative' : 'r-positive'}
                >
                  {rec.kind === 'avoid' ? '⛔' : '✅'} {rec.text}{' '}
                  <span className="muted">({prettyKey(rec.scope, rec.key)})</span>
                </li>
              ))}
            </ol>
          )}
          <p className="muted" data-testid="coach-setups-time">
            Denní doba (Praha): nejlepší{' '}
            {best && time.best_segment
              ? `${best.label} ${formatR(best.avg_r)} (n=${best.n})`
              : '— (malý vzorek)'}
            , nejhorší{' '}
            {worst && time.worst_segment
              ? `${worst.label} ${formatR(worst.avg_r)} (n=${worst.n})`
              : '— (malý vzorek)'}
            .
            {flags.length > 0 && (
              <>
                {' '}
                Příznaky:{' '}
                {flags
                  .map(([, stat]) => `${stat.label} ${stat.n}× (Σ ${formatR(stat.sum_r)})`)
                  .join(' · ')}
                .
              </>
            )}
          </p>
        </>
      )}
    </section>
  )
}
