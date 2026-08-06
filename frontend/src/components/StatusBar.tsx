/** Stavová lišta pipeline (SPEC 3.7 + 7.1) — živě ze status kanálu /ws/live. */
import { daysLabel, useFaAlpha } from '../hooks/useFaAlpha'
import { useAppState } from '../state/AppState'

function formatBytes(bytes?: number): string {
  if (bytes === undefined) return '—'
  const units = ['B', 'kB', 'MB', 'GB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`
}

export function StatusBar() {
  const { status, symbol } = useAppState()
  // Kalibrovaná FA α (#232 fáze 2) — badge jen když už existuje první bod
  const faAlpha = useFaAlpha(symbol)
  const greeks =
    status.greeks_complete !== undefined && status.greeks_total !== undefined
      ? `Greeks ${status.greeks_complete}/${status.greeks_total}`
      : 'Greeks —'
  const repair =
    (status.repair_count ?? 0) > 0
      ? `Repair: retrying ${status.repair_count} incomplete strikes`
      : null
  const lines =
    status.lines_utilization !== undefined
      ? `Lines ${Math.round(status.lines_utilization * 100)} %`
      : null
  const disk = `Disk ${formatBytes(status.disk_usage_bytes)} / ${formatBytes(status.disk_limit_bytes)}`
  const connection = status.connection ?? status.engine
  const liveStamp = status.last_tick_ts
    ? `● Live ${status.last_tick_ts}`
    : status.engine === 'online'
      ? '● Live'
      : 'Stale'

  return (
    <footer className="status-bar" aria-label="Stav pipeline">
      <span data-testid="status-greeks">{greeks}</span>
      {repair && <span data-testid="status-repair">{repair}</span>}
      {lines && <span data-testid="status-lines">{lines}</span>}
      <span data-testid="status-disk">{disk}</span>
      {faAlpha && (
        <span
          data-testid="status-fa-alpha"
          title={
            'Kalibrovaná α flow-adjusted odhadu OI (ADR-0011): medián poměru skutečného ' +
            'ΔOI k net klasifikovanému toku z ranních validací, EMA přes dny. ' +
            'Počet = kolik čistých dnů už kalibrace započetla.'
          }
        >
          FA α={faAlpha.alpha.toFixed(2)} · {daysLabel(faAlpha.days)}
        </span>
      )}
      <span data-testid="status-ibkr">
        IBKR: {connection}
        {status.port !== undefined ? ` :${status.port}` : ''}
      </span>
      <span data-testid="status-live">{liveStamp}</span>
    </footer>
  )
}
