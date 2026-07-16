/** Globální stav aplikace: pipeline status z WS, instrument, expirace, přepínače. */
import { createContext, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { LiveSocket } from '../api/ws'
import { API_BASE, WS_URL } from '../config'

export interface PipelineStatus {
  engine: string
  connection?: string
  port?: number
  greeks_complete?: number
  greeks_total?: number
  repair_count?: number
  lines_utilization?: number
  disk_usage_bytes?: number
  disk_limit_bytes?: number
  last_tick_ts?: string
  updated_at?: number | null
}

export interface Toggles {
  dynGex: boolean
  gexLevels: boolean
  sessions: boolean
  vol: boolean
  optVol: boolean
  delta: boolean
  volOiDelta: boolean
  news: boolean
}

interface AppState {
  status: PipelineStatus
  symbol: string
  expiries: string[]
  selectedExpiry: string | null
  setSelectedExpiry: (expiry: string) => void
  timeframe: 'intraday' | 'daily'
  setTimeframe: (value: 'intraday' | 'daily') => void
  interval: '1m' | '5m' | '15m'
  setInterval: (value: '1m' | '5m' | '15m') => void
  toggles: Toggles
  setToggle: (key: keyof Toggles, value: boolean) => void
}

const AppStateContext = createContext<AppState | null>(null)

export function AppStateProvider({
  children,
  socket,
  symbol = 'ES',
}: {
  children: ReactNode
  /** Testovatelnost: injektovaný LiveSocket místo výchozího. */
  socket?: LiveSocket
  symbol?: string
}) {
  const [status, setStatus] = useState<PipelineStatus>({ engine: 'offline' })
  const [expiries, setExpiries] = useState<string[]>([])
  const [selectedExpiry, setSelectedExpiry] = useState<string | null>(null)
  const [timeframe, setTimeframe] = useState<'intraday' | 'daily'>('intraday')
  const [interval, setInterval] = useState<'1m' | '5m' | '15m'>('1m')
  const [toggles, setToggles] = useState<Toggles>({
    dynGex: true,
    gexLevels: true,
    sessions: false,
    vol: true,
    optVol: true,
    delta: true,
    volOiDelta: true,
    news: false,
  })

  useEffect(() => {
    const live = socket ?? new LiveSocket(WS_URL)
    live.subscribe('status', (data) => setStatus(data as unknown as PipelineStatus))
    live.connect()
    return () => live.close()
  }, [socket])

  useEffect(() => {
    let cancelled = false
    fetch(`${API_BASE}/instruments/${symbol}/expiries`)
      .then((response) => (response.ok ? response.json() : { expiries: [] }))
      .then((payload: { expiries: string[] }) => {
        if (cancelled) return
        setExpiries(payload.expiries)
        setSelectedExpiry(payload.expiries[0] ?? null)
      })
      .catch(() => {
        // API neběží — hlavička ukáže placeholder, status bar offline stav
        if (!cancelled) setExpiries([])
      })
    return () => {
      cancelled = true
    }
  }, [symbol])

  const value = useMemo<AppState>(
    () => ({
      status,
      symbol,
      expiries,
      selectedExpiry,
      setSelectedExpiry,
      timeframe,
      setTimeframe,
      interval,
      setInterval,
      toggles,
      setToggle: (key, val) => setToggles((prev) => ({ ...prev, [key]: val })),
    }),
    [status, symbol, expiries, selectedExpiry, timeframe, interval, toggles],
  )

  return <AppStateContext.Provider value={value}>{children}</AppStateContext.Provider>
}

export function useAppState(): AppState {
  const state = useContext(AppStateContext)
  if (state === null) {
    throw new Error('useAppState musí být uvnitř AppStateProvider')
  }
  return state
}
