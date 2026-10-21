/** Stavová lišta pipeline (SPEC 3.7 + 7.1) — živě ze status kanálu /ws/live. */
import { daysLabel, faAlphaBadge, useFaAlpha, type FaAlphaBadge } from '../hooks/useFaAlpha'
import { useAppState } from '../state/AppState'

/** Tooltip badge FA α — odrážky a `\n` (vzor `ivRankTooltip`). */
function faAlphaTooltip(badge: FaAlphaBadge): string {
  if (badge.kind === 'uncalibrated') {
    return [
      `FA odhad OI je vypnutý do kalibrace z tisků (ADR-0057 bod 6): ${badge.days} z ${badge.minDays} platných dnů.`,
      '• α se kalibruje z čistého objemu tisků se stranou agresora od burzy',
      '• den s mezerou tisků za otevřeného trhu bod nedá',
      '• stav z midpoint testu (před mechanikou v6) je jiná veličina a nepočítá se',
      '• do té doby engine FA vrstvu (odhad OI, FA levels, FA profil) nepíše',
    ].join('\n')
  }
  return [
    'Kalibrovaná α flow-adjusted odhadu OI (ADR-0011):',
    '• medián poměru skutečného ΔOI k čistému toku z ranních validací, EMA přes dny',
    '• počet = kolik čistých dnů už kalibrace započetla',
    badge.source === 'aggressor'
      ? '• čistý tok z tisků se stranou agresora od burzy (ADR-0057)'
      : '• čistý tok z midpoint testu (mechanika v5)',
  ].join('\n')
}

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
  // Kalibrovaná FA α (#232 fáze 2); pod tisky „nekalibrováno n/5“ (ADR-0057 bod 6)
  const faAlpha = faAlphaBadge(
    useFaAlpha(),
    symbol,
    status.cumdelta_source,
    Object.keys(status.cumdelta_coverage ?? {}),
  )
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
        <span data-testid="status-fa-alpha" title={faAlphaTooltip(faAlpha)}>
          {faAlpha.kind === 'calibrated'
            ? `FA α=${faAlpha.alpha.toFixed(2)} · ${daysLabel(faAlpha.days)}`
            : `FA nekalibrováno ${faAlpha.days}/${faAlpha.minDays}`}
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
