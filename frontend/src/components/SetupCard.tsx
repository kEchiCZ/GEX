/** Karta aktivního setupu nad grafem (ADR-0004): entry/cíl/stop, RRR, zdůvodnění. */
import { memo } from 'react'
import { setupRrr, templateLabel } from '../api/setups'
import type { SetupRow } from '../api/setups'
import { formatLevel } from '../heatmap/overlays'
import { positionLabel, positionSize } from '../instrument/position'

/** Čas vzniku setupu (ISO) → lokální datum + čas; prázdné, když chybí/nevalidní. */
function setupTimestamp(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleString([], {
        day: '2-digit',
        month: '2-digit',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })
}

function SetupCardBase({
  setups,
  onDismiss,
  riskAccountUsd = 0,
  riskPct = 0,
}: {
  setups: SetupRow[]
  onDismiss: (id: number) => void
  /** Kalkulačka pozice (#679): účet a % rizika ze Settings → Trading; 0 = skrýt. */
  riskAccountUsd?: number
  riskPct?: number
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
          {(() => {
            // Kalkulačka pozice (#679): čistě klientský výpočet, nic na server
            const size = positionSize({
              symbol: setup.symbol,
              entry: setup.entry,
              stop: setup.stop,
              accountUsd: riskAccountUsd,
              riskPct,
            })
            return size === null ? null : (
              <div className="setup-card-position muted">{positionLabel(size, riskPct)}</div>
            )
          })()}
          {setupTimestamp(setup.created_ts) && (
            <div className="setup-card-time muted">Vznik {setupTimestamp(setup.created_ts)}</div>
          )}
          <p className="setup-card-reason">{setup.reason}</p>
        </div>
      ))}
    </div>
  )
}

export const SetupCard = memo(SetupCardBase)
