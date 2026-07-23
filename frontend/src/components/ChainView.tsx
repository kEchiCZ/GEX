/** Greeks & OI tabulka (#202): per-strike pohled na opční řetěz vybrané expirace.

Živý pohled z poslední minuty snapshotů (GET /chain, refresh à 60 s) — call
strana vlevo, strike uprostřed, put vpravo (klasické chain rozložení). ATM
řádek zvýrazněný, stale strany ztlumené, ΔOI vs. poslední archivovaný den.
*/
import { useEffect, useMemo, useState } from 'react'
import { API_BASE } from '../config'
import { useAppState } from '../state/AppState'

const REFRESH_MS = 60_000

interface ChainSide {
  bid: number | null
  ask: number | null
  last: number | null
  volume: number
  iv: number | null
  delta: number | null
  gamma: number | null
  theta: number | null
  vega: number | null
  oi: number
  oi_change: number | null
  stale: boolean
}

interface ChainRow {
  strike: number
  call?: ChainSide
  put?: ChainSide
}

function fmt(value: number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined) return '—'
  return value.toFixed(decimals)
}

function fmtSigned(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const rounded = Math.round(value)
  return rounded > 0 ? `+${rounded}` : String(rounded)
}

const SIDE_HEADERS = ['Bid', 'Ask', 'Last', 'Vol', 'IV', 'Δ', 'Γ', 'Θ', 'Vega', 'OI', 'ΔOI']

function SideCells({ side, mirror }: { side?: ChainSide; mirror: boolean }) {
  const cells = side
    ? [
        fmt(side.bid),
        fmt(side.ask),
        fmt(side.last),
        String(Math.round(side.volume)),
        side.iv === null ? '—' : `${(side.iv * 100).toFixed(1)} %`,
        fmt(side.delta, 3),
        fmt(side.gamma, 4),
        fmt(side.theta, 2),
        fmt(side.vega, 2),
        String(Math.round(side.oi)),
        fmtSigned(side.oi_change),
      ]
    : SIDE_HEADERS.map(() => '—')
  // Put strana zrcadlově (ΔOI…Bid), ať OI sloupce obou stran sousedí se strikem
  const ordered = mirror ? [...cells].reverse() : cells
  return (
    <>
      {ordered.map((text, index) => (
        <td key={index} className={side?.stale ? 'stale' : undefined}>
          {text}
        </td>
      ))}
    </>
  )
}

export function ChainView() {
  const { symbol, selectedExpiry, priceInfo } = useAppState()
  const [rows, setRows] = useState<ChainRow[]>([])
  const [ts, setTs] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const today = useMemo(() => new Date().toISOString().slice(0, 10), [])

  useEffect(() => {
    if (!selectedExpiry) return
    let cancelled = false
    const load = () => {
      fetch(`${API_BASE}/chain/${symbol}/${selectedExpiry}?date=${today}`)
        .then((response) => {
          if (!response.ok) throw new Error(`HTTP ${response.status}`)
          return response.json()
        })
        .then((payload: { ts: string; rows: ChainRow[] }) => {
          if (cancelled) return
          setRows(payload.rows)
          setTs(payload.ts)
          setError(null)
        })
        .catch((cause: Error) => {
          if (!cancelled) setError(`Řetěz nejde načíst: ${cause.message}`)
        })
    }
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [symbol, selectedExpiry, today])

  // Nejvyšší strike nahoře (stejná orientace jako heatmapa); ATM = nejblíž ceně
  const ordered = useMemo(() => [...rows].sort((a, b) => b.strike - a.strike), [rows])
  const spot = priceInfo.last
  const atmStrike = useMemo(() => {
    if (spot === null || ordered.length === 0) return null
    return ordered.reduce((best, row) =>
      Math.abs(row.strike - spot) < Math.abs(best.strike - spot) ? row : best,
    ).strike
  }, [ordered, spot])

  return (
    <main className="chain-view" aria-label="Greeks & OI tabulka">
      <header className="chain-header">
        <strong>
          {symbol} · {selectedExpiry ?? '—'}
        </strong>
        <span className="muted">
          {ts ? `data ${new Date(ts).toLocaleTimeString()}` : 'čekám na data…'}
          {' · živý pohled (poslední minuta), obnovuje se à 60 s'}
        </span>
      </header>
      {error && <p className="muted">{error}</p>}
      <div className="chain-scroll">
        <table className="chain-table">
          <thead>
            <tr>
              <th colSpan={SIDE_HEADERS.length} className="side-call">
                Call
              </th>
              <th className="strike-col">Strike</th>
              <th colSpan={SIDE_HEADERS.length} className="side-put">
                Put
              </th>
            </tr>
            <tr>
              {SIDE_HEADERS.map((header) => (
                <th key={`c-${header}`}>{header}</th>
              ))}
              <th className="strike-col" />
              {[...SIDE_HEADERS].reverse().map((header) => (
                <th key={`p-${header}`}>{header}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ordered.map((row) => (
              <tr key={row.strike} className={row.strike === atmStrike ? 'atm' : undefined}>
                <SideCells side={row.call} mirror={false} />
                <td className="strike-col">{row.strike}</td>
                <SideCells side={row.put} mirror />
              </tr>
            ))}
          </tbody>
        </table>
        {ordered.length === 0 && !error && <p className="muted">Žádná data pro dnešní den.</p>}
      </div>
    </main>
  )
}
