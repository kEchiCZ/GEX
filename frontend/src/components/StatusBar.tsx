/** Stavová lišta pipeline (SPEC 3.7 + 7.1) — živě ze status kanálu /ws/live. */
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
  const { status } = useAppState()
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
      <span data-testid="status-ibkr">
        IBKR: {connection}
        {status.port !== undefined ? ` :${status.port}` : ''}
      </span>
      <span data-testid="status-live">{liveStamp}</span>
    </footer>
  )
}
