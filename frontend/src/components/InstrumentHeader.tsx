/** Hlavička instrumentu (SPEC 7.1): ticker, last + změna, expirace, Live, notifikace. */
import { useEffect, useState } from 'react'
import { expiryCountdown, expiryKind } from '../instrument/expiry'
import { useAppState } from '../state/AppState'

/** Čas alertu (unix s) → lokální datum + čas; prázdné, když ts chybí/nevalidní. */
function alertTimestamp(ts: number): string {
  if (!Number.isFinite(ts)) return ''
  return new Date(ts * 1000).toLocaleString([], {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** Zobrazovací názvy běžných futures podkladů (jinak jen ticker). */
const SYMBOL_NAMES: Record<string, string> = {
  ES: 'E-mini S&P 500',
  NQ: 'E-mini Nasdaq-100',
  RTY: 'E-mini Russell 2000',
  YM: 'E-mini Dow',
  CL: 'Crude Oil',
  GC: 'Gold',
}

export function InstrumentHeader({
  lastPrice,
  changePct,
}: {
  lastPrice?: number
  changePct?: number
}) {
  const {
    symbol,
    expiries,
    selectedExpiry,
    setSelectedExpiry,
    status,
    alerts,
    unreadAlerts,
    markAlertsRead,
  } = useAppState()
  const [alertsOpen, setAlertsOpen] = useState(false)
  const live = status.engine === 'online'
  // Odpočet do expirace se obnovuje po minutě (velké expirace = velké OI, Moodix workflow)
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), 60_000)
    return () => clearInterval(timer)
  }, [])
  const kind = selectedExpiry ? expiryKind(selectedExpiry) : null
  const countdown = selectedExpiry ? expiryCountdown(selectedExpiry, now) : null

  return (
    <header className="instrument-header">
      <div className="instrument-title">
        <span className="ticker">{symbol}</span>
        <span className="name muted">{SYMBOL_NAMES[symbol] ?? ''}</span>
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
      {kind && (
        <span className="muted expiry-meta" data-testid="expiry-meta">
          {kind}
          {countdown && ` · expiruje ${countdown}`}
        </span>
      )}
      <span className={live ? 'live-indicator live' : 'live-indicator stale'} role="status">
        {live ? '● Live' : '○ Offline'}
      </span>
      <div className="bell-wrap">
        <button
          className="bell"
          aria-label={`Notifikace (${unreadAlerts})`}
          onClick={() => {
            setAlertsOpen((open) => !open)
            markAlertsRead()
          }}
        >
          🔔{unreadAlerts > 0 && <span className="badge">{unreadAlerts}</span>}
        </button>
        {alertsOpen && (
          <div className="alerts-dropdown" role="dialog" aria-label="Historie alertů">
            {alerts.length === 0 && <p className="muted">Žádné alerty</p>}
            <ol>
              {[...alerts].reverse().map((alert, index) => {
                const stamp = alertTimestamp(alert.ts)
                // Zvoneček je globální (napříč instrumenty) → u alertu i symbol
                const tag = [alert.symbol, alert.kind].filter(Boolean).join(' · ')
                return (
                  <li key={index}>
                    {stamp && <time className="alert-time muted">{stamp}</time>}
                    <span className="muted">[{tag}]</span> {alert.message}
                  </li>
                )
              })}
            </ol>
          </div>
        )}
      </div>
    </header>
  )
}
