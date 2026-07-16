/** Globální stav aplikace: pipeline status z WS, view, téma, alerty, přepínače. */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
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
  news_available?: boolean
  updated_at?: number | null
}

export interface AlertMessage {
  kind: string
  symbol: string
  message: string
  ts: number
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

export type AppView = 'chart' | 'dashboard' | 'console' | 'settings'
export type Theme = 'dark' | 'light'

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
  view: AppView
  setView: (view: AppView) => void
  theme: Theme
  setTheme: (theme: Theme) => void
  alerts: AlertMessage[]
  unreadAlerts: number
  markAlertsRead: () => void
  consoleLog: string[]
}

const AppStateContext = createContext<AppState | null>(null)

const LOG_LIMIT = 200
const ALERTS_LIMIT = 50

const VIEWS: readonly AppView[] = ['chart', 'dashboard', 'console', 'settings']

/** Deep-link: počáteční obrazovka a téma z URL (?view=dashboard&theme=light). */
function initialFromUrl(): { view: AppView; theme: Theme } {
  const params = typeof window !== 'undefined' ? new URLSearchParams(window.location.search) : null
  const view = params?.get('view')
  const theme = params?.get('theme')
  return {
    view: VIEWS.includes(view as AppView) ? (view as AppView) : 'chart',
    theme: theme === 'light' ? 'light' : 'dark',
  }
}

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
  const [view, setView] = useState<AppView>(() => initialFromUrl().view)
  const [theme, setTheme] = useState<Theme>(() => initialFromUrl().theme)
  const [alerts, setAlerts] = useState<AlertMessage[]>([])
  const [unreadAlerts, setUnreadAlerts] = useState(0)
  const [consoleLog, setConsoleLog] = useState<string[]>([])
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

  const appendLog = useCallback((line: string) => {
    const stamp = new Date().toLocaleTimeString()
    setConsoleLog((previous) => [...previous.slice(-(LOG_LIMIT - 1)), `[${stamp}] ${line}`])
  }, [])

  useEffect(() => {
    const live = socket ?? new LiveSocket(WS_URL)
    live.subscribe('status', (data) => {
      setStatus(data as unknown as PipelineStatus)
      const record = data as Record<string, unknown>
      appendLog(
        `status: engine=${String(record.engine)} connection=${String(record.connection ?? '—')}`,
      )
    })
    live.subscribe('alerts', (data) => {
      const alert = data as unknown as AlertMessage
      setAlerts((previous) => [...previous.slice(-(ALERTS_LIMIT - 1)), alert])
      setUnreadAlerts((previous) => previous + 1)
      appendLog(`alert [${alert.kind}] ${alert.message}`)
    })
    live.connect()
    return () => live.close()
  }, [socket, appendLog])

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
      view,
      setView,
      theme,
      setTheme,
      alerts,
      unreadAlerts,
      markAlertsRead: () => setUnreadAlerts(0),
      consoleLog,
    }),
    [
      status,
      symbol,
      expiries,
      selectedExpiry,
      timeframe,
      interval,
      toggles,
      view,
      theme,
      alerts,
      unreadAlerts,
      consoleLog,
    ],
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
