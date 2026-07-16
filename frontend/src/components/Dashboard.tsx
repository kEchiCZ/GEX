/** Dashboard (SPEC 7.5): karty watchlistu s mini NetGEX profilem a stavem dat. */
import { useEffect, useState } from 'react'
import { API_BASE } from '../config'
import { useAppState } from '../state/AppState'
import type { ProfileRow } from '../profile/bars'

interface WatchlistItem {
  id: number
  symbol: string
}

function MiniProfile({ rows }: { rows: ProfileRow[] }) {
  if (rows.length === 0) return <p className="muted">bez dat</p>
  const width = 180
  const height = 48
  const barWidth = width / rows.length
  const peak = Math.max(
    1e-9,
    ...rows.map((row) =>
      Math.abs(
        row.callVolComponent + row.callOiComponent - row.putVolComponent - row.putOiComponent,
      ),
    ),
  )
  return (
    <svg width={width} height={height} aria-label="Mini NetGEX profil">
      {rows.map((row, index) => {
        const net =
          row.callVolComponent + row.callOiComponent - row.putVolComponent - row.putOiComponent
        const barHeight = (Math.abs(net) / peak) * (height / 2)
        return (
          <rect
            key={row.strike}
            x={index * barWidth}
            y={net >= 0 ? height / 2 - barHeight : height / 2}
            width={Math.max(1, barWidth - 0.5)}
            height={Math.max(0.5, barHeight)}
            fill={net >= 0 ? '#14b8a6' : '#ef4444'}
          />
        )
      })}
    </svg>
  )
}

export function Dashboard({
  profileRows,
  spot,
  callWall,
  putWall,
}: {
  profileRows: ProfileRow[]
  spot: number | null
  callWall: number | null
  putWall: number | null
}) {
  const { status, symbol: activeSymbol } = useAppState()
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([])

  useEffect(() => {
    let cancelled = false
    fetch(`${API_BASE}/watchlist`)
      .then((response) => (response.ok ? response.json() : { watchlist: [] }))
      .then((payload: { watchlist: WatchlistItem[] }) => {
        if (!cancelled) setWatchlist(payload.watchlist)
      })
      .catch(() => {
        // API neběží — dashboard ukáže aspoň aktivní instrument
        if (!cancelled) setWatchlist([])
      })
    return () => {
      cancelled = true
    }
  }, [])

  const symbols = watchlist.length > 0 ? watchlist.map((item) => item.symbol) : [activeSymbol]

  return (
    <main className="dashboard" aria-label="Dashboard">
      {symbols.map((symbol) => {
        const isActive = symbol === activeSymbol
        return (
          <article key={symbol} className="dashboard-card" aria-label={`Karta ${symbol}`}>
            <header>
              <strong>{symbol}</strong>
              <span className={status.engine === 'online' ? 'change-up' : 'muted'}>
                {status.engine === 'online' ? '● live' : 'offline'}
              </span>
            </header>
            <div className="card-price">{isActive && spot !== null ? spot.toFixed(2) : '—'}</div>
            <MiniProfile rows={isActive ? profileRows : []} />
            <footer className="muted">
              {isActive && spot !== null && callWall !== null && putWall !== null ? (
                <>
                  call wall {callWall.toFixed(0)} ({(callWall - spot).toFixed(1)}) · put wall{' '}
                  {putWall.toFixed(0)} ({(putWall - spot).toFixed(1)})
                </>
              ) : (
                'walls —'
              )}
            </footer>
          </article>
        )
      })}
    </main>
  )
}
