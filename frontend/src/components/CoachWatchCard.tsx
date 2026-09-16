/** Briefing → karta Kouč (#1201): na co si dnes dát pozor — pravidla z týdne,
prodělečná okna dne, doporučení pro setupy. Bez dat se nekreslí. */
import { useEffect, useState } from 'react'
import { fetchCoachSummary } from '../api/coach'
import type { CoachSummary } from '../api/coach'

export function CoachWatchCard({ symbol }: { symbol: string }) {
  const [summary, setSummary] = useState<CoachSummary | null>(null)
  useEffect(() => {
    let cancelled = false
    void fetchCoachSummary(symbol).then((result) => {
      if (!cancelled) setSummary(result)
    })
    return () => {
      cancelled = true
    }
  }, [symbol])
  if (summary === null || (summary.watch.length === 0 && summary.lines.length === 0)) return null
  return (
    <section className="briefing-card briefing-coach" aria-label="Kouč — na co si dnes dát pozor">
      <h3>🎓 Kouč — na co si dnes dát pozor</h3>
      {summary.watch.length > 0 ? (
        <ol className="coach-rules" data-testid="coach-watch">
          {summary.watch.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ol>
      ) : (
        <p className="muted">Zatím bez pravidel — málo obchodů v deníku.</p>
      )}
      {summary.lines.length > 0 && <p className="muted">{summary.lines[0]}</p>}
    </section>
  )
}
