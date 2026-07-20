/** Denní dataset: /replay balík z API, fallback na demo data (API/engine neběží).

Timeframe 'daily' skládá sloupec za každý uložený den (seznam z /instruments/…/days,
každý den má vlastní expiraci — 0DTE řetěz).
*/
import { useEffect, useMemo, useState } from 'react'
import type { PanelSeries } from '../components/BottomPanels'
import type { LiveSocket, ChannelData } from '../api/ws'
import { demoGrid, demoOverlays, demoPanels, demoProfile } from '../heatmap/demo'
import type { HeatmapGrid } from '../heatmap/grid'
import type { RawDay } from '../heatmap/modes'
import type { OverlayData, PriceBar } from '../heatmap/overlays'
import type { ProfileRow } from '../profile/bars'
import { autoSessions } from '../instrument/sessions'
import { buildDailyDay } from './daily'
import {
  appendMinute,
  assembleReplayDay,
  fetchDays,
  fetchReplay,
  fetchReplayInputs,
} from './loader'
import type { LiveMinute, LiveMinuteRow, ReplayDay, ReplayInputs } from './loader'

/** Daily pohled: strop stažených dnů (retence snapshotů je 14 dní, R4). */
const DAILY_MAX_DAYS = 14

/** Plný refetch balíku je jen HODINOVÁ pojistka — živě jede append z WS kanálů (#127). */
const LIVE_RECONCILE_MS = 3_600_000
/** Debounce sběru WS kanálů jedné minuty (engine je pošle v rámci ~1 s). */
const APPEND_DEBOUNCE_MS = 400

/** Kanonický klíč minuty — sjednotí `ts`/`ts_min` napříč kanály (ISO s .000Z). */
function minuteKey(value: unknown): string {
  const date = new Date(String(value))
  return Number.isNaN(date.getTime()) ? String(value) : date.toISOString()
}

function numOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** Rozdělaná svíčka aktuální (neuzavřené) minuty z živého spotu (#128). */
interface FormingBar {
  minuteIso: string
  open: number
  high: number
  low: number
  close: number
}

/** Čas na začátek minuty (UTC) — sladí spot ts s klíči minut (ts_min). */
function floorMinuteIso(value: unknown): string {
  const date = new Date(String(value))
  if (Number.isNaN(date.getTime())) return String(value)
  date.setUTCSeconds(0, 0)
  return date.toISOString()
}

/** Přidá rozdělanou svíčku na náběžnou hranu (jen je-li její minuta za posledními daty). */
function withForming(day: ReplayDay, forming: FormingBar | null): ReplayDay {
  if (!forming) return day
  const lastMinute = day.minutes.at(-1)
  if (lastMinute !== undefined && forming.minuteIso <= lastMinute) return day // už uzavřená minuta
  const bar: PriceBar = {
    minuteIdx: day.grid.minutes, // jeden koš za posledním uzavřeným → náběžná hrana
    open: forming.open,
    high: forming.high,
    low: forming.low,
    close: forming.close,
    up: forming.close >= forming.open,
  }
  return {
    ...day,
    minutes: [...day.minutes, forming.minuteIso],
    overlays: { ...day.overlays, price: [...(day.overlays.price ?? []), bar] },
  }
}

export interface DayData {
  source: 'replay' | 'demo'
  grid: HeatmapGrid
  /** Surová snapshot matice (přepínání módů/škál) — jen intraday replay. */
  raw: RawDay | null
  overlays: OverlayData
  panels: PanelSeries
  profileByMinute: ProfileRow[][] | null // demo má jediný statický profil
  demoProfileRows: ProfileRow[] | null
  spotSeries: (number | null)[]
  /** Popisky časové osy (HH:MM lokálního času) per minuta. */
  minuteLabels: string[]
}

function demoDay(): DayData {
  const grid = demoGrid()
  const overlays = demoOverlays(grid)
  const spotSeries = Array.from({ length: grid.minutes }, (_, minuteIdx) => {
    const bar = overlays.price?.find((item) => item.minuteIdx === minuteIdx)
    return bar ? bar.close : null
  })
  // Demo den simuluje RTH 9:30–16:00 (390 minut)
  const minuteLabels = Array.from({ length: grid.minutes }, (_, minuteIdx) => {
    const total = 9 * 60 + 30 + minuteIdx
    const hours = Math.floor(total / 60)
    const mins = total % 60
    return `${hours}:${String(mins).padStart(2, '0')}`
  })
  return {
    source: 'demo',
    grid,
    raw: null,
    overlays,
    panels: demoPanels(grid.minutes),
    profileByMinute: null,
    demoProfileRows: demoProfile(grid),
    spotSeries,
    minuteLabels,
  }
}

function replayToDay(day: ReplayDay): DayData {
  const spotSeries: (number | null)[] = Array.from({ length: day.grid.minutes }, () => null)
  for (const bar of day.overlays.price ?? []) {
    spotSeries[bar.minuteIdx] = bar.close
  }
  const minuteLabels = day.minutes.map((iso) => {
    const parsed = new Date(iso)
    return Number.isNaN(parsed.getTime())
      ? iso
      : parsed.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  })
  return {
    source: 'replay',
    grid: day.grid,
    raw: day.raw,
    // Seance markery se generují automaticky z časů minut (pevné UTC časy burz)
    overlays: { ...day.overlays, sessions: autoSessions(day.minutes) },
    panels: day.panels,
    profileByMinute: day.profileByMinute,
    demoProfileRows: null,
    spotSeries,
    minuteLabels,
  }
}

export function useDayData(
  symbol: string,
  expiry: string | null,
  date: string,
  timeframe: 'intraday' | 'daily',
  socket?: LiveSocket,
): DayData {
  const fallback = useMemo(() => demoDay(), [])
  const [inputs, setInputs] = useState<ReplayInputs | null>(null)
  const [daily, setDaily] = useState<DayData | null>(null)
  // Živý spot (#128): rozdělaná svíčka aktuální minuty (aktualizuje se sub-sekundově)
  const [forming, setForming] = useState<FormingBar | null>(null)

  // Změna instrumentu/expirace: starý dataset nesmí přežít (jiný symbol → demo/nový fetch)
  useEffect(() => {
    setInputs(null)
    setDaily(null)
    setForming(null)
  }, [symbol, expiry, date])

  // Jakmile se rozdělaná minuta uzavře (dorazí její snapshot), přestává být rozdělaná
  useEffect(() => {
    if (forming && inputs?.minutes.includes(forming.minuteIso)) setForming(null)
  }, [inputs, forming])

  const [retry, setRetry] = useState(0)
  // Hodinová pojistka: plný refetch srovná mezery / OI archiv / stale opravy (#127).
  const [reconcileTick, setReconcileTick] = useState(0)
  useEffect(() => {
    if (timeframe !== 'intraday') return
    const id = setInterval(() => setReconcileTick((n) => n + 1), LIVE_RECONCILE_MS)
    return () => clearInterval(id)
  }, [timeframe])

  // Úvodní / rekonciliační fetch celého balíku
  useEffect(() => {
    if (!expiry || timeframe !== 'intraday') return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    fetchReplayInputs(symbol, expiry, date)
      .then((loaded) => {
        // Prázdný den (0 minut) nesmí přepsat poslední živý stav při přechodném výpadku
        if (!cancelled && loaded.minutes.length > 0) setInputs(loaded)
      })
      .catch(() => {
        // Den (zatím) neexistuje — např. čerstvě přidaný ticker; zkusit znovu za 30 s
        if (!cancelled) timer = setTimeout(() => setRetry((n) => n + 1), 30_000)
      })
    return () => {
      cancelled = true
      if (timer !== null) clearTimeout(timer)
    }
  }, [symbol, expiry, date, timeframe, retry, reconcileTick])

  // Živý append z WS kanálů (#127): snapshot/price/levels/flow → jedna minuta
  useEffect(() => {
    if (!socket || !expiry || timeframe !== 'intraday') return
    const pending = new Map<string, Partial<LiveMinute>>()
    let flushTimer: ReturnType<typeof setTimeout> | null = null

    const scheduleFlush = () => {
      if (flushTimer !== null) return
      flushTimer = setTimeout(() => {
        flushTimer = null
        if (pending.size === 0) return
        setInputs((prev) => {
          if (!prev) return prev
          let next = prev
          for (const ts of [...pending.keys()].sort((a, b) => (a < b ? -1 : 1))) {
            const partial = pending.get(ts)!
            // Aplikuj minutu, když má snapshot řez (nová/aktualizace mřížky) NEBO už existuje
            // (dorazil jen bar/levels/flow k uzavřené minutě — jinak by svíčka chyběla, #133).
            if (partial.rows || next.minutes.includes(ts)) {
              next = appendMinute(next, {
                tsIso: ts,
                rows: partial.rows ?? [],
                bar: partial.bar,
                levels: partial.levels,
                flow: partial.flow,
              })
              pending.delete(ts)
            }
          }
          return next
        })
      }, APPEND_DEBOUNCE_MS)
    }
    const part = (ts: string): Partial<LiveMinute> => {
      const existing = pending.get(ts)
      if (existing) return existing
      const fresh: Partial<LiveMinute> = {}
      pending.set(ts, fresh)
      return fresh
    }

    const onSnapshot = (data: ChannelData) => {
      const raw = Array.isArray(data.rows) ? (data.rows as Record<string, unknown>[]) : []
      part(minuteKey(data.ts_min)).rows = raw.map((row): LiveMinuteRow => ({
        strike: Number(row.strike),
        right: String(row.right) === 'C' ? 'C' : 'P',
        oi: Number(row.oi) || 0,
        volume: Number(row.volume) || 0,
        delta: Number(row.delta) || 0,
        stale_age: Number(row.stale_age) || 0,
      }))
      scheduleFlush()
    }
    const onPrice = (data: ChannelData) => {
      const close = Number(data.close)
      if (!Number.isFinite(close)) return
      part(minuteKey(data.ts)).bar = {
        open: Number(data.open),
        high: Number(data.high),
        low: Number(data.low),
        close,
        volume: Number(data.volume) || 0,
      }
      scheduleFlush()
    }
    const onLevels = (data: ChannelData) => {
      part(minuteKey(data.ts_min)).levels = {
        flip: numOrNull(data.flip),
        centroid: numOrNull(data.centroid),
        call_wall: numOrNull(data.call_wall),
        put_wall: numOrNull(data.put_wall),
      }
      scheduleFlush()
    }
    const onFlow = (data: ChannelData) => {
      part(minuteKey(data.ts_min)).flow = { cum_delta: Number(data.cum_delta) || 0 }
      scheduleFlush()
    }
    // Živý spot (#128): rozdělaná svíčka aktuální minuty — jen overlay, bez přepočtu gridu
    const onSpot = (data: ChannelData) => {
      const price = Number(data.price)
      if (!Number.isFinite(price)) return
      const minuteIso = floorMinuteIso(data.ts)
      setForming((prev) =>
        prev && prev.minuteIso === minuteIso
          ? {
              ...prev,
              high: Math.max(prev.high, price),
              low: Math.min(prev.low, price),
              close: price,
            }
          : { minuteIso, open: price, high: price, low: price, close: price },
      )
    }

    const snapshotCh = `snapshot.${symbol}.${expiry}`
    const priceCh = `price.${symbol}`
    const levelsCh = `levels.${symbol}.${expiry}`
    const flowCh = `flow.${symbol}`
    const spotCh = `spot.${symbol}`
    socket.subscribe(snapshotCh, onSnapshot)
    socket.subscribe(priceCh, onPrice)
    socket.subscribe(levelsCh, onLevels)
    socket.subscribe(flowCh, onFlow)
    socket.subscribe(spotCh, onSpot)
    // Reconnect: dofetchni celý balík (mohli jsme zmeškat minuty)
    const offReconnect = socket.onReconnect(() => setReconcileTick((n) => n + 1))
    return () => {
      if (flushTimer !== null) clearTimeout(flushTimer)
      socket.unsubscribe(snapshotCh, onSnapshot)
      socket.unsubscribe(priceCh, onPrice)
      socket.unsubscribe(levelsCh, onLevels)
      socket.unsubscribe(flowCh, onFlow)
      socket.unsubscribe(spotCh, onSpot)
      offReconnect()
    }
  }, [symbol, expiry, timeframe, socket])

  useEffect(() => {
    if (timeframe !== 'daily') return
    let cancelled = false
    fetchDays(symbol)
      .then(async (listing) => {
        const recent = listing.slice(-DAILY_MAX_DAYS)
        const results = await Promise.allSettled(
          recent.map((day) => fetchReplay(symbol, day.expiry, day.date)),
        )
        const days = results
          .filter(
            (result): result is PromiseFulfilledResult<ReplayDay> =>
              result.status === 'fulfilled' && result.value.grid.minutes > 0,
          )
          .map((result) => result.value)
        if (!cancelled && days.length > 0) setDaily(buildDailyDay(days))
      })
      .catch(() => {
        // API neběží — zůstává demo dataset
      })
    return () => {
      cancelled = true
    }
  }, [symbol, timeframe])

  // Grid se skládá jen při změně dat (drahé); rozdělaná svíčka je levná vrstva nad ním.
  const baseReplay = useMemo(() => (inputs ? assembleReplayDay(inputs) : null), [inputs])
  const replayDay = useMemo(
    () => (baseReplay ? replayToDay(withForming(baseReplay, forming)) : null),
    [baseReplay, forming],
  )
  if (timeframe === 'daily') return daily ?? fallback
  return replayDay ?? fallback
}
