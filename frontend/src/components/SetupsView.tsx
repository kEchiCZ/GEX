/** Obrazovka Setupy (ADR-0004): historie analýz s výsledky a ručním hodnocením.

Predikce jsou neměnné — jediná mutace je rating (+1/−1) a poznámka; hodnocení
je kvalitativní vrstva a nevstupuje do automatické kalibrace confidence.
*/
import { useState } from 'react'
import { ACCOUNT_START_USD, STATUS_LABELS, formatPct, formatPnlUsd, reviewSetup, setupPnlPct, setupPnlUsd, setupRrr, templateLabel } from '../api/setups' // prettier-ignore
import type { SetupRow } from '../api/setups'
import { formatLevel } from '../heatmap/overlays'
import { useSetups } from '../hooks/useSetups'
import { pointValue } from '../instrument/tick'
import { useAppState } from '../state/AppState'

function formatTs(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString()
}

function ReviewCell({
  row,
  symbol,
  onSaved,
}: {
  row: SetupRow
  symbol: string
  onSaved: () => void
}) {
  const [note, setNote] = useState(row.user_note ?? '')
  const [saving, setSaving] = useState(false)

  const save = async (rating: 1 | -1 | null) => {
    setSaving(true)
    const ok = await reviewSetup(symbol, row.id, rating, note.trim() === '' ? null : note.trim())
    setSaving(false)
    if (ok) onSaved()
  }

  if (row.status === 'active') return <span className="muted">běží</span>
  return (
    <div className="setup-review">
      <button
        className={row.user_rating === 1 ? 'chip active' : 'chip'}
        aria-label={`Setup ${row.id} vyšel`}
        title="Setup vyšel podle predikce"
        disabled={saving}
        onClick={() => void save(row.user_rating === 1 ? null : 1)}
      >
        👍
      </button>
      <button
        className={row.user_rating === -1 ? 'chip active' : 'chip'}
        aria-label={`Setup ${row.id} nevyšel`}
        title="Setup nevyšel / byl zavádějící"
        disabled={saving}
        onClick={() => void save(row.user_rating === -1 ? null : -1)}
      >
        👎
      </button>
      <input
        value={note}
        placeholder="Poznámka"
        aria-label={`Poznámka k setupu ${row.id}`}
        maxLength={500}
        onChange={(event) => setNote(event.target.value)}
        onBlur={() => {
          if ((row.user_note ?? '') !== note.trim()) void save((row.user_rating as 1 | -1) ?? null)
        }}
      />
    </div>
  )
}

export function SetupsView() {
  const { symbol } = useAppState()
  const { setups, refresh } = useSetups()

  const closed = setups.filter((row) => row.status !== 'active')
  const wins = closed.filter((row) => (row.outcome_r ?? 0) > 0).length
  const totalR = closed.reduce((sum, row) => sum + (row.outcome_r ?? 0), 0)
  // P/L v USD na 1 kontrakt (#185) — CME hodnota bodu instrumentu
  const pointUsd = pointValue(symbol)
  const totalPnl = closed.reduce((sum, row) => sum + (setupPnlUsd(row, pointUsd) ?? 0), 0)
  // % P/L vůči startovnímu účtu 5 000 $ na ticker (#191) — s fixní bází je
  // součet procent setupů roven celkovému zhodnocení účtu
  const totalPct = (totalPnl / ACCOUNT_START_USD) * 100
  const averageR = closed.length > 0 ? totalR / closed.length : 0
  const pnlClass = totalPnl >= 0 ? 'r-positive' : 'r-negative'

  return (
    <section className="setups-view" aria-label="Setupy">
      <header className="setups-summary">
        <h2>Setupy — {symbol}</h2>
      </header>
      {/* Zvýrazněné souhrnné statistiky (#189); P/L vždy na 1 kontrakt */}
      <div className="setups-stats" role="group" aria-label="Souhrnné statistiky">
        <div className="stat">
          <span className="stat-label muted">Aktivní</span>
          <span className="stat-value">{setups.length - closed.length}</span>
        </div>
        <div className="stat">
          <span className="stat-label muted">Uzavřené</span>
          <span className="stat-value">{closed.length}</span>
        </div>
        <div className="stat">
          <span className="stat-label muted">Úspěšnost</span>
          <span className="stat-value">
            {closed.length > 0 ? `${Math.round((wins / closed.length) * 100)} %` : '—'}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label muted">Ø R</span>
          <span className={`stat-value ${averageR >= 0 ? 'r-positive' : 'r-negative'}`}>
            {closed.length > 0 ? `${averageR >= 0 ? '+' : ''}${averageR.toFixed(2)}` : '—'}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label muted">Σ P/L (1 kontrakt)</span>
          <span className={`stat-value ${pnlClass}`} data-testid="setups-total-pnl">
            {closed.length > 0 ? formatPnlUsd(totalPnl) : '—'}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label muted">% P/L (účet 5k)</span>
          <span className={`stat-value ${pnlClass}`} data-testid="setups-total-pct">
            {closed.length > 0 ? formatPct(totalPct) : '—'}
          </span>
        </div>
      </div>
      {setups.length === 0 && (
        <p className="muted">
          Zatím žádné setupy — detektor běží nad živými daty a čeká na podmínky šablon (odraz od
          zdi, neúspěšný průraz, Max Pain pin, gamma momentum).
        </p>
      )}
      {setups.length > 0 && (
        <div className="setups-table-wrap">
          <table className="setups-table">
            <thead>
              <tr>
                <th>Vznik</th>
                <th>Šablona</th>
                <th>Směr</th>
                <th>Entry</th>
                <th>Cíl</th>
                <th>Stop</th>
                <th>RRR</th>
                <th>Důvěra</th>
                <th>Stav</th>
                <th>Uzavřeno</th>
                <th>R</th>
                <th>P/L (1 ks)</th>
                <th>Hodnocení</th>
              </tr>
            </thead>
            <tbody>
              {setups.map((row) => {
                const pnl = setupPnlUsd(row, pointUsd)
                const pct = setupPnlPct(row, pointUsd)
                return (
                  <tr key={row.id} title={row.reason}>
                    <td>{formatTs(row.created_ts)}</td>
                    <td>{templateLabel(row.template)}</td>
                    <td className={row.direction}>{row.direction === 'long' ? 'LONG' : 'SHORT'}</td>
                    <td>{formatLevel(row.entry)}</td>
                    <td>{formatLevel(row.target)}</td>
                    <td>{formatLevel(row.stop)}</td>
                    <td>{setupRrr(row).toFixed(1)}</td>
                    <td>{row.confidence} %</td>
                    <td>
                      <span className={`setup-status ${row.status}`}>
                        {STATUS_LABELS[row.status] ?? row.status}
                      </span>
                    </td>
                    <td data-part="closed-ts">{formatTs(row.closed_ts)}</td>
                    <td className={(row.outcome_r ?? 0) >= 0 ? 'r-positive' : 'r-negative'}>
                      {row.outcome_r === null
                        ? '—'
                        : `${row.outcome_r >= 0 ? '+' : ''}${row.outcome_r.toFixed(2)}`}
                    </td>
                    <td className={(pnl ?? 0) >= 0 ? 'r-positive' : 'r-negative'} data-part="pnl">
                      {pnl === null ? '—' : formatPnlUsd(pnl)}
                      {pct !== null && <span className="pnl-pct muted"> {formatPct(pct)}</span>}
                    </td>
                    <td>
                      <ReviewCell row={row} symbol={symbol} onSaved={refresh} />
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted setups-disclaimer">
        Setupy jsou podpora rozhodování, ne obchodní signály. Confidence se kalibruje až s dostatkem
        uzavřených výsledků (Fáze 2).
      </p>
    </section>
  )
}
