/** Hlavička instrumentu (SPEC 7.1): ticker, last + změna, expirace, Live indikátor, zvonek. */
import { useAppState } from '../state/AppState'

export function InstrumentHeader({
  lastPrice,
  changePct,
  alertCount = 0,
}: {
  lastPrice?: number
  changePct?: number
  alertCount?: number
}) {
  const { symbol, expiries, selectedExpiry, setSelectedExpiry, status } = useAppState()
  const live = status.engine === 'online'

  return (
    <header className="instrument-header">
      <div className="instrument-title">
        <span className="ticker">{symbol}</span>
        <span className="name muted">E-mini S&amp;P 500</span>
      </div>
      <div className="instrument-price">
        <span className="last">{lastPrice !== undefined ? lastPrice.toFixed(2) : '—'}</span>
        {changePct !== undefined && (
          <span className={changePct >= 0 ? 'change-up' : 'change-down'}>
            {changePct >= 0 ? '+' : ''}
            {changePct.toFixed(2)} %
          </span>
        )}
      </div>
      <label className="expiry-select">
        Expirace
        <select
          value={selectedExpiry ?? ''}
          onChange={(event) => setSelectedExpiry(event.target.value)}
          disabled={expiries.length === 0}
        >
          {expiries.length === 0 && <option value="">—</option>}
          {expiries.map((expiry) => (
            <option key={expiry} value={expiry}>
              {expiry}
            </option>
          ))}
        </select>
      </label>
      <span className={live ? 'live-indicator live' : 'live-indicator stale'} role="status">
        {live ? '● Live' : '○ Offline'}
      </span>
      <button className="bell" aria-label={`Notifikace (${alertCount})`}>
        🔔{alertCount > 0 && <span className="badge">{alertCount}</span>}
      </button>
    </header>
  )
}
