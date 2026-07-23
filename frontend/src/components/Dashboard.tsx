/** Dashboard (SPEC 7.5): karty watchlistu s mini NetGEX profilem a stavem dat. */
import { useEffect, useState } from 'react'
import { API_BASE } from '../config'
import { REGIME_HINTS, REGIME_LABELS } from '../instrument/regime'
import { formatPcr } from '../instrument/sentiment'
import type { PcrPoint } from '../instrument/sentiment'
import { useAppState } from '../state/AppState'
import type { ProfileRow } from '../profile/bars'

interface WatchlistItem {
  id: number
  symbol: string
}

/** Mini křivka PCR(volume) za den (#205): 2px linka, šedá reference na 1.0.

Jedna série → bez legendy (název nese popisek vedle); hodnoty v textových
tónech, barvu nese jen značka — konvence dataviz.
*/
function PcrSparkline({ series }: { series: (number | null)[] }) {
  const width = 180
  const height = 28
  const values = series.filter((value): value is number => value !== null)
  if (values.length < 2) return null
  const min = Math.min(...values, 1)
  const max = Math.max(...values, 1)
  const span = Math.max(1e-9, max - min)
  const toY = (value: number) => height - 2 - ((value - min) / span) * (height - 4)
  const step = width / Math.max(1, series.length - 1)
  let path = ''
  series.forEach((value, index) => {
    if (value === null) return
    const point = `${(index * step).toFixed(1)},${toY(value).toFixed(1)}`
    path += path === '' ? `M${point}` : `L${point}`
  })
  return (
    <svg width={width} height={height} aria-label="Vývoj PCR (volume) za den">
      <line
        x1={0}
        x2={width}
        y1={toY(1)}
        y2={toY(1)}
        stroke="rgba(125,133,150,0.55)"
        strokeDasharray="3 3"
      />
      <path d={path} fill="none" stroke="#14b8a6" strokeWidth={2} strokeLinejoin="round" />
    </svg>
  )
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
  pcr = { volume: null, oi: null },
  pcrSeries = [],
}: {
  profileRows: ProfileRow[]
  spot: number | null
  callWall: number | null
  putWall: number | null
  /** Put/Call ratio aktivního instrumentu (#205); ostatní karty data nemají. */
  pcr?: PcrPoint
  pcrSeries?: (number | null)[]
}) {
  const { status, symbol: activeSymbol, regimeInfo } = useAppState()
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
            {isActive && regimeInfo.state && (
              // GEX režim (#209) — jen aktivní instrument (data ostatních nejsou v paměti)
              <span
                className={`regime-badge regime-${regimeInfo.state}`}
                title={REGIME_HINTS[regimeInfo.state]}
              >
                {REGIME_LABELS[regimeInfo.state]}
              </span>
            )}
            {isActive && (pcr.volume !== null || pcr.oi !== null) && (
              // Sentiment (#205): PCR z vlastních dat — vol = dnešní tok,
              // OI = držený stav; rozdíl = tok proti pozicování
              <div
                className="card-pcr muted"
                data-testid="card-pcr"
                title="Put/Call ratio z našich dat. Volume > 1 a roste = defenzivní tok (nákupy putů), extrémy fungují kontrariánsky. OI = strukturální pozicování; rozdíl volume vs. OI = dnešní tok proti drženému stavu. Křivka = vývoj PCR (volume) za den, čárkovaná reference = 1.0."
              >
                <span>
                  PCR vol <strong>{formatPcr(pcr.volume)}</strong> · OI{' '}
                  <strong>{formatPcr(pcr.oi)}</strong>
                </span>
                <PcrSparkline series={pcrSeries} />
              </div>
            )}
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
