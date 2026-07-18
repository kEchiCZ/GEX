/** Karta aktivního setupu nad grafem (ADR-0004): entry/cíl/stop, RRR, zdůvodnění. */
import { setupRrr, templateLabel } from '../api/setups'
import type { SetupRow } from '../api/setups'
import { formatLevel } from '../heatmap/overlays'

export function SetupCard({
  setups,
  onDismiss,
}: {
  setups: SetupRow[]
  onDismiss: (id: number) => void
}) {
  if (setups.length === 0) return null
  return (
    <div className="setup-cards" aria-label="Aktivní setupy">
      {setups.map((setup) => (
        <div key={setup.id} className={`setup-card ${setup.direction}`} role="status">
          <div className="setup-card-head">
            <strong>
              {setup.direction === 'long' ? 'LONG' : 'SHORT'} · {templateLabel(setup.template)}
            </strong>
            <button
              className="setup-card-dismiss"
              aria-label={`Skrýt setup ${setup.id}`}
              title="Skrýt kartu (setup dál běží; historie v obrazovce Setupy)"
              onClick={() => onDismiss(setup.id)}
            >
              ×
            </button>
          </div>
          <div className="setup-card-levels">
            <span className="entry">Entry {formatLevel(setup.entry)}</span>
            <span className="target">Cíl {formatLevel(setup.target)}</span>
            <span className="stop">Stop {formatLevel(setup.stop)}</span>
          </div>
          <div className="setup-card-meta">
            RRR {setupRrr(setup).toFixed(1)} · důvěra {setup.confidence} %
          </div>
          <p className="setup-card-reason">{setup.reason}</p>
        </div>
      ))}
    </div>
  )
}
