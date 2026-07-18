/** Obrazovka Setupy (ADR-0004): historie analýz s výsledky a ručním hodnocením.

Predikce jsou neměnné — jediná mutace je rating (+1/−1) a poznámka; hodnocení
je kvalitativní vrstva a nevstupuje do automatické kalibrace confidence.
*/
import { useState } from 'react'
import { STATUS_LABELS, reviewSetup, setupRrr, templateLabel } from '../api/setups'
import type { SetupRow } from '../api/setups'
import { formatLevel } from '../heatmap/overlays'
import { useSetups } from '../hooks/useSetups'
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

  return (
    <section className="setups-view" aria-label="Setupy">
      <header className="setups-summary">
        <h2>Setupy — {symbol}</h2>
        <span className="muted">
          {setups.filter((row) => row.status === 'active').length} aktivních · {closed.length}{' '}
          uzavřených
          {closed.length > 0 &&
            ` · úspěšnost ${Math.round((wins / closed.length) * 100)} % · Σ ${totalR.toFixed(1)} R`}
        </span>
      </header>
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
                <th>R</th>
                <th>Hodnocení</th>
              </tr>
            </thead>
            <tbody>
              {setups.map((row) => (
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
                  <td className={(row.outcome_r ?? 0) >= 0 ? 'r-positive' : 'r-negative'}>
                    {row.outcome_r === null
                      ? '—'
                      : `${row.outcome_r >= 0 ? '+' : ''}${row.outcome_r.toFixed(2)}`}
                  </td>
                  <td>
                    <ReviewCell row={row} symbol={symbol} onSaved={refresh} />
                  </td>
                </tr>
              ))}
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
