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

/** Poslední cena + denní změna (hlavička; plní MainContent z denních dat). */
export interface PriceInfo {
  last: number | null
  changePct: number | null
}

/** Intraday timeframy — agregace 1m dat do košů (SPEC 7.1, TradingView sada). */
export const INTERVALS = [
  '1m',
  '2m',
  '3m',
  '5m',
  '10m',
  '15m',
  '30m',
  '45m',
  '1h',
  '2h',
  '3h',
  '4h',
  '1d',
] as const
export type Interval = (typeof INTERVALS)[number]

export const INTERVAL_MINUTES: Record<Interval, number> = {
  '1m': 1,
  '2m': 2,
  '3m': 3,
  '5m': 5,
  '10m': 10,
  '15m': 15,
  '30m': 30,
  '45m': 45,
  '1h': 60,
  '2h': 120,
  '3h': 180,
  '4h': 240,
  '1d': 1440,
}

interface AppState {
  status: PipelineStatus
  symbol: string
  /** Přepnutí aktivního tickeru (z watchlistu v sidebaru). */
  setSymbol: (symbol: string) => void
  expiries: string[]
  selectedExpiry: string | null
  setSelectedExpiry: (expiry: string) => void
  timeframe: 'intraday' | 'daily'
  setTimeframe: (value: 'intraday' | 'daily') => void
  interval: Interval
  setInterval: (value: Interval) => void
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
  priceInfo: PriceInfo
  setPriceInfo: (info: PriceInfo) => void
}

const AppStateContext = createContext<AppState | null>(null)

const LOG_LIMIT = 200
const ALERTS_LIMIT = 50

const VIEWS: readonly AppView[] = ['chart', 'dashboard', 'console', 'settings']

/** Výchozí expirace: dnešní (0DTE řetěz), jinak nejnovější — první dir může být včerejšek. */
export function defaultExpiry(expiries: string[]): string | null {
  if (expiries.length === 0) return null
  const today = new Date().toISOString().slice(0, 10).replaceAll('-', '')
  return expiries.includes(today) ? today : (expiries.at(-1) ?? null)
}

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
  symbol: initialSymbol = 'ES',
}: {
  children: ReactNode
  /** Testovatelnost: injektovaný LiveSocket místo výchozího. */
  socket?: LiveSocket
  symbol?: string
}) {
  const [status, setStatus] = useState<PipelineStatus>({ engine: 'offline' })
  const [symbol, setSymbol] = useState(initialSymbol)
  const [expiries, setExpiries] = useState<string[]>([])
  const [selectedExpiry, setSelectedExpiry] = useState<string | null>(null)
  const [timeframe, setTimeframe] = useState<'intraday' | 'daily'>('intraday')
  const [interval, setInterval] = useState<Interval>('1m')
  const [view, setView] = useState<AppView>(() => initialFromUrl().view)
  const [theme, setTheme] = useState<Theme>(() => initialFromUrl().theme)
  const [alerts, setAlerts] = useState<AlertMessage[]>([])
  const [unreadAlerts, setUnreadAlerts] = useState(0)
  const [consoleLog, setConsoleLog] = useState<string[]>([])
  const [priceInfo, setPriceInfoState] = useState<PriceInfo>({ last: null, changePct: null })
  // Bail-out na stejné hodnoty — pojistka proti render smyčce při nestabilních identitách
  const setPriceInfo = useCallback((info: PriceInfo) => {
    setPriceInfoState((previous) =>
      previous.last === info.last && previous.changePct === info.changePct ? previous : info,
    )
  }, [])
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

  // Počáteční stav pipeline hned z REST — WS push chodí až s dalším cyklem enginu (~60 s)
  useEffect(() => {
    let cancelled = false
    fetch(`${API_BASE}/status`)
      .then((response) => (response.ok ? response.json() : null))
      .then((payload: PipelineStatus | null) => {
        if (!cancelled && payload) setStatus(payload)
      })
      .catch(() => {
        // API neběží — zůstává offline stav
      })
    return () => {
      cancelled = true
    }
  }, [])

  const [expiryRetry, setExpiryRetry] = useState(0)
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    // Čerstvě přidaný ticker nemusí mít ještě data — bez expirací zkoušet à 30 s
    const scheduleRetry = () => {
      timer = setTimeout(() => setExpiryRetry((n) => n + 1), 30_000)
    }
    fetch(`${API_BASE}/instruments/${symbol}/expiries`)
      .then((response) => (response.ok ? response.json() : { expiries: [] }))
      .then((payload: { expiries: string[] }) => {
        if (cancelled) return
        setExpiries(payload.expiries)
        setSelectedExpiry(defaultExpiry(payload.expiries))
        if (payload.expiries.length === 0) scheduleRetry()
      })
      .catch(() => {
        // API neběží — hlavička ukáže placeholder, status bar offline stav
        if (!cancelled) {
          setExpiries([])
          setSelectedExpiry(null)
          scheduleRetry()
        }
      })
    return () => {
      cancelled = true
      if (timer !== null) clearTimeout(timer)
    }
  }, [symbol, expiryRetry])

  const value = useMemo<AppState>(
    () => ({
      status,
      symbol,
      setSymbol,
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
      priceInfo,
      setPriceInfo,
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
      priceInfo,
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
