/** Stats → Releasy: předem registrované hypotézy a jejich živý track record (#1296, ADR-0044).

Hypotézy a kritéria se po registraci nemění; počítají se jen releasy od data
registrace a rozhoduje se jen v kontrolních bodech. Chyba načtení je vidět,
ne prázdná tabulka. */
import { useEffect, useState } from 'react'
import { fetchReleaseHypotheses, headingTooltip, releaseDay } from '../api/releases'
import type { HypothesisStatus, ReleaseHypotheses } from '../api/releases'

const STATUS_LABELS: Record<HypothesisStatus, string> = {
  testing: 'ověřuje se',
  verified: 'ověřeno',
  rejected: 'zamítnuto',
}

const FAMILY_LABELS: Record<string, string> = {
  RETAIL: 'Retail Sales',
  ISM_SERVICES: 'ISM Services',
}

function percent(value: number | null): string {
  return value === null ? '—' : `${Math.round(value * 100)} %`
}

export function ReleaseHypothesesSection() {
  const [data, setData] = useState<ReleaseHypotheses | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    fetchReleaseHypotheses()
      .then((payload) => {
        if (!cancelled) setData(payload)
      })
      .catch((failure: unknown) => {
        if (cancelled) return
        setError(failure instanceof Error ? failure.message : String(failure))
      })
    return () => {
      cancelled = true
    }
  }, [])
  const rows = (data?.hypotheses ?? []).flatMap((hypothesis) =>
    Object.entries(hypothesis.symbols).map(([symbol, state]) => ({ hypothesis, symbol, state })),
  )
  const anyLive = rows.some((row) => row.state.n > 0)
  return (
    <section className="stats-section" aria-label="Hypotézy releasů">
      <h2 title={data ? headingTooltip(data) : undefined}>
        Releasy — předem registrované hypotézy (#1296)
      </h2>
      <p className="muted">
        Upozornění před releasem nese směr jen u jádra inflace (H1) a do ověření bez
        pravděpodobnosti. Tady je živý stav každé hypotézy — historie výzkumu je jen popisná.
      </p>
      {error !== null ? (
        <p className="hypothesis-error" role="alert" data-testid="release-hypotheses-error">
          Hypotézy releasů se nepodařilo načíst: {error}
        </p>
      ) : data === null ? (
        <p className="muted">Načítám…</p>
      ) : (
        <>
          {!anyLive && (
            <p className="muted" data-testid="release-hypotheses-empty">
              Živě zatím žádný hodnocený release (registrace {releaseDay(data.registered_at)}).
            </p>
          )}
          <table className="stats-table" data-testid="release-hypotheses">
            <thead>
              <tr>
                <th>Hypotéza</th>
                <th>Instrument</th>
                <th>Stav</th>
                <th>Živě k/n</th>
                <th>95% interval</th>
                <th>Rozhodnutí při n</th>
                <th>Historicky (popisně)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ hypothesis, symbol, state }) => (
                <tr
                  key={`${hypothesis.id}-${symbol}`}
                  data-testid={`hyp-${hypothesis.id}-${symbol}`}
                >
                  <td title={`${hypothesis.label}\n\n• ${hypothesis.rule}`}>
                    <strong>{hypothesis.id}</strong> {hypothesis.label}
                    {state.outcomes.length > 0 && (
                      <details>
                        <summary>výsledky ({state.outcomes.length}, max. 10 posledních)</summary>
                        <ul>
                          {state.outcomes
                            .slice(-10)
                            .reverse()
                            .map((outcome) => (
                              <li key={outcome.cluster_ts}>
                                {releaseDay(outcome.cluster_ts)}{' '}
                                {FAMILY_LABELS[outcome.family] ?? outcome.family}{' '}
                                {outcome.hit ? '✔' : '✘'} {outcome.value_bp.toFixed(1)} bp
                              </li>
                            ))}
                        </ul>
                      </details>
                    )}
                  </td>
                  <td>{symbol}</td>
                  <td>
                    <span className={`hypothesis-chip hypothesis-${state.status}`}>
                      {STATUS_LABELS[state.status]}
                    </span>
                  </td>
                  <td>
                    {state.hits}/{state.n}
                  </td>
                  <td>
                    {state.n > 0 ? `${percent(state.wilson_lb)}–${percent(state.wilson_ub)}` : '—'}
                  </td>
                  <td>
                    {state.decided_at_n !== null
                      ? `rozhodnuto při ${state.decided_at_n}`
                      : state.next_checkpoint !== null
                        ? `další při ${state.next_checkpoint}`
                        : '—'}
                  </td>
                  <td>
                    {state.historical.hits}/{state.historical.n}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  )
}
