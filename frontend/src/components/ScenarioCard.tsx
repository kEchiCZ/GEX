/** Karta scénáře (#1173): snímek, cíle, termín, výsledek — Briefing i Stats. */
import { scenarioImageUrl, verdictLabel } from '../api/scenarios'
import type { Scenario } from '../api/scenarios'

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleString([], {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function ScenarioCard({
  scenario,
  compact = false,
}: {
  scenario: Scenario
  compact?: boolean
}) {
  const result = scenario.result
  const cls = result
    ? `scenario-verdict scenario-${result.verdict}`
    : 'scenario-verdict scenario-pending'
  return (
    <article className="scenario-card" data-testid={`scenario-${scenario.id}`}>
      <div className="scenario-head">
        <strong>
          {scenario.symbol} · {fmtTime(scenario.created_at)}
        </strong>
        <span className="muted"> → termín {scenario.deadline}</span>
        <span className={cls}>{verdictLabel(result)}</span>
        <span
          className="scenario-source"
          title={
            scenario.source === 'auto'
              ? 'Založil engine z verdiktu dne (15 min před US openem)'
              : 'Nakreslený ručně (✎ Scénář)'
          }
        >
          {scenario.source === 'auto' ? 'auto' : 'ručně'}
        </span>
      </div>
      <div className="muted">
        vstup {scenario.entry.toFixed(2)} · cíle{' '}
        {scenario.targets.map((t) => t.toFixed(2)).join(' → ')}
        {scenario.note ? ` · ${scenario.note}` : ''}
      </div>
      {scenario.rationale && (
        <details className="scenario-rationale">
          <summary className="muted">
            verdikt {scenario.rationale.verdict} · skóre {scenario.rationale.score >= 0 ? '+' : ''}
            {scenario.rationale.score}
            {scenario.rationale.targets ? ` · cíle ${scenario.rationale.targets.join(' → ')}` : ''}
          </summary>
          <ul>
            {scenario.rationale.votes.map((vote) => (
              <li key={vote.name}>
                {vote.vote >= 0 ? '+' : ''}
                {vote.vote} {vote.reason}
              </li>
            ))}
            {scenario.rationale.missing.length > 0 && (
              <li className="muted">chybělo: {scenario.rationale.missing.join(', ')}</li>
            )}
          </ul>
        </details>
      )}
      {result && (
        <div className="muted" data-testid={`scenario-result-${scenario.id}`}>
          cíl 1 {result.hit1 ? 'ano' : 'ne'}
          {result.hit2 !== null ? ` · cíl 2 ${result.hit2 ? 'ano' : 'ne'}` : ''}
          {result.order_ok !== null ? ` · pořadí ${result.order_ok ? 'drží' : 'ne'}` : ''}
          {result.max_dev_pts !== null ? ` · max. odchylka ${result.max_dev_pts.toFixed(1)} b` : ''}
          {result.max_dev_em !== null ? ` (${result.max_dev_em.toFixed(2)} EM)` : ''}
          {scenario.evaluated_at === null ? '' : ` · vyhodnoceno ${fmtTime(scenario.evaluated_at)}`}
        </div>
      )}
      {scenario.evaluated_at !== null && result === null && (
        <div className="muted">bez barů v okně — nešlo posoudit</div>
      )}
      {scenario.has_image && !compact && (
        <a href={scenarioImageUrl(scenario.id)} target="_blank" rel="noreferrer">
          <img
            className="scenario-image"
            src={scenarioImageUrl(scenario.id)}
            alt={`Snímek scénáře ${scenario.id}`}
            loading="lazy"
          />
        </a>
      )}
    </article>
  )
}
