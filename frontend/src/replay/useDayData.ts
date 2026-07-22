/** Denní dataset: /replay balík z API, fallback na demo data (API/engine neběží).

Timeframe 'daily' skládá sloupec za každý uložený den (seznam z /instruments/…/days,
každý den má vlastní expiraci — 0DTE řetěz).
*/
import { useEffect, useMemo, useRef, useState } from 'react'
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
import type { GexProfileRow, LiveMinute, LiveMinuteRow, ProfileSource, ReplayDay, ReplayInputs } from './loader' // prettier-ignore

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

/** Svíčka odvozená ze živého spotu (#128): rozdělaná minuta, nebo záloha uzavřené
minuty, které ještě nedorazil skutečný bar z price kanálu (#143). */
interface SpotBar {
  minuteIso: string
  open: number
  high: number
  low: number
  close: number
}

/** Strop držených spot svíček — jen pojistka proti neomezenému růstu; minuty
s finálním barem uklízí efekt níž. Při výpadku SAMOTNÉHO price kanálu (WS drží,
reconnect-refetch nenastane) drží zálohy až hodinu do rekonciliace (#159). */
const SPOT_BARS_KEEP = 60

/** Čas na začátek minuty (UTC) — sladí spot ts s klíči minut (ts_min). */
function floorMinuteIso(value: unknown): string {
  const date = new Date(String(value))
  if (Number.isNaN(date.getTime())) return String(value)
  date.setUTCSeconds(0, 0)
  return date.toISOString()
}

function toPriceBar(spot: SpotBar, minuteIdx: number, previousClose: number): PriceBar {
  return {
    minuteIdx,
    open: spot.open,
    high: spot.high,
    low: spot.low,
    close: spot.close,
    // Stejná sémantika jako statické bary: směr vůči close předchozí svíčky,
    // bez ní vůči vlastnímu open (#159) — jinak živá svíčka po uzavření mění barvu
    up: Number.isNaN(previousClose) ? spot.close >= spot.open : !(spot.close < previousClose),
  }
}

/** Popisek minuty na časové ose (lokální HH:MM).

Formátovač se drží jeden sdílený a hotové popisky se cachují per ISO: `toLocaleTimeString`
staví nový `Intl.DateTimeFormat` při každém volání a přepočítával se celý den při každém
appendu minuty — přes 120 minut to byla většina nákladu skládání dne (#142). */
const MINUTE_FORMATTER = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' })
const minuteLabelCache = new Map<string, string>()
/** Strop cache — přes noc/přepínání dnů by jinak rostla bez omezení. */
const MINUTE_LABEL_CACHE_MAX = 5000

export function minuteLabel(iso: string): string {
  const cached = minuteLabelCache.get(iso)
  if (cached !== undefined) return cached
  const parsed = new Date(iso)
  const label = Number.isNaN(parsed.getTime()) ? iso : MINUTE_FORMATTER.format(parsed)
  if (minuteLabelCache.size >= MINUTE_LABEL_CACHE_MAX) minuteLabelCache.clear()
  minuteLabelCache.set(iso, label)
  return label
}

/** Živá (dynamická) vrstva ceny — vše, co se mění sub-sekundově ze spot kanálu.
Drží se MIMO `DayData`, aby statická data zůstala identitou stabilní i při 5 ticích/s
a nepřekreslovala statickou vrstvu overlaye (#141). */
export interface LiveOverlay {
  /** Svíčky ze spotu, které nejsou v `overlays.price` — rozdělaná minuta + zálohy (#143). */
  bars: PriceBar[]
  /** Popisky minut za koncem gridu; index = `minuteIdx - grid.minutes`. */
  labels: string[]
}

/** Sdílená prázdná vrstva — stabilní identita pro dny bez živého spotu. */
export const EMPTY_LIVE: LiveOverlay = { bars: [], labels: [] }

/** Rozdělí spot svíčky na živou vrstvu: zálohy uzavřených minut bez skutečného baru
(#143) a rozdělané minuty za koncem gridu (náběžná hrana, #128). */
function splitSpotBars(day: ReplayDay, spotBars: SpotBar[]): LiveOverlay {
  if (spotBars.length === 0) return EMPTY_LIVE
  const minuteIndex = new Map(day.minutes.map((iso, index) => [iso, index]))
  // Provizorní bar (ADR-0005) minutu nepokrývá — rozdělaná svíčka ze spotu je živější
  const provisional = new Set(day.provisionalMinutes)
  const covered = new Set(
    (day.overlays.price ?? [])
      .map((bar) => bar.minuteIdx)
      .filter((minuteIdx) => !provisional.has(minuteIdx)),
  )
  const lastMinute = day.minutes.at(-1)
  // Close nejbližší statické svíčky PŘED danou minutou (směr/barva svíčky, #159)
  const priceBars = day.overlays.price ?? []
  const closeBefore = (minuteIdx: number): number => {
    for (let index = priceBars.length - 1; index >= 0; index -= 1) {
      if (priceBars[index].minuteIdx < minuteIdx) return priceBars[index].close
    }
    return Number.NaN
  }
  const bars: PriceBar[] = []
  const labels: string[] = []
  for (const spot of [...spotBars].sort((a, b) => (a.minuteIso < b.minuteIso ? -1 : 1))) {
    const existingIdx = minuteIndex.get(spot.minuteIso)
    if (existingIdx !== undefined) {
      // Uzavřená minuta bez skutečného baru → záložní svíčka ze spotu
      if (!covered.has(existingIdx)) {
        bars.push(toPriceBar(spot, existingIdx, bars.at(-1)?.close ?? closeBefore(existingIdx)))
      }
    } else if (lastMinute === undefined || spot.minuteIso > lastMinute) {
      // Minuta za posledními daty (rozdělaná, nebo čeká na snapshot) → náběžná hrana
      const minuteIdx = day.grid.minutes + labels.length
      bars.push(toPriceBar(spot, minuteIdx, bars.at(-1)?.close ?? closeBefore(minuteIdx)))
      labels.push(minuteLabel(spot.minuteIso))
    }
  }
  return bars.length === 0 ? EMPTY_LIVE : { bars, labels }
}

export interface DayData {
  source: 'replay' | 'demo'
  grid: HeatmapGrid
  /** Surová snapshot matice (přepínání módů/škál) — jen intraday replay. */
  raw: RawDay | null
  overlays: OverlayData
  panels: PanelSeries
  profileByMinute: ProfileSource | null // demo má jediný statický profil
  demoProfileRows: ProfileRow[] | null
  spotSeries: (number | null)[]
  /** Popisky časové osy (HH:MM lokálního času) per minuta. */
  minuteLabels: string[]
  /** ISO čas poslední naměřené minuty — horizont projekce (ADR-0006). */
  lastMinuteIso: string | null
  /** Dyn GEX profil per minuta/koš (ADR-0009); null = bez profilů. */
  gexProfile: (GexProfileRow | null)[] | null
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
    lastMinuteIso: null, // demo den není ukotvený v čase → bez projekce
    gexProfile: null,
  }
}

function replayToDay(day: ReplayDay): DayData {
  const spotSeries: (number | null)[] = Array.from({ length: day.grid.minutes }, () => null)
  for (const bar of day.overlays.price ?? []) {
    spotSeries[bar.minuteIdx] = bar.close
  }
  const minuteLabels = day.minutes.map(minuteLabel)
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
    lastMinuteIso: day.minutes.at(-1) ?? null,
    gexProfile: day.gexProfile,
  }
}

/** Denní data rozdělená na statickou (`day`) a živou (`live`) část — viz #141. */
export interface DayFeed {
  day: DayData
  live: LiveOverlay
}

export function useDayData(
  symbol: string,
  expiry: string | null,
  date: string,
  timeframe: 'intraday' | 'daily',
  socket?: LiveSocket,
): DayFeed {
  const fallback = useMemo(() => demoDay(), [])
  const [inputs, setInputs] = useState<ReplayInputs | null>(null)
  // Zrcadlo inputs pro rozhodování ve flushi mimo setState updater (#143: updater musí být čistý)
  const inputsRef = useRef<ReplayInputs | null>(null)
  useEffect(() => {
    inputsRef.current = inputs
  }, [inputs])
  const [daily, setDaily] = useState<DayData | null>(null)
  // Živý spot (#128, #143): rozdělaná svíčka aktuální minuty + záložní bary minut bez skutečného baru
  const [spotBars, setSpotBars] = useState<SpotBar[]>([])

  // Změna instrumentu/expirace: starý dataset nesmí přežít (jiný symbol → demo/nový fetch)
  useEffect(() => {
    setInputs(null)
    setDaily(null)
    setSpotBars([])
  }, [symbol, expiry, date])

  // Jakmile pro minutu dorazí FINÁLNÍ bar (price kanál), spot záloha končí.
  // Pouhý snapshot minuty nestačí — jinak by svíčka do příchodu baru chyběla (#143).
  // Provizorní bar ji taky neruší, aby rozdělaná minuta zůstala živá (ADR-0005).
  useEffect(() => {
    if (spotBars.length === 0 || !inputs) return
    const withFinalBar = new Set(
      inputs.bars.filter((bar) => bar.final !== false).map((bar) => bar.tsIso),
    )
    if (spotBars.some((spot) => withFinalBar.has(spot.minuteIso))) {
      setSpotBars((previous) => previous.filter((spot) => !withFinalBar.has(spot.minuteIso)))
    }
  }, [inputs, spotBars])

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
        const current = inputsRef.current
        if (!current) return // balík ještě nedorazil — minuty počkají na další flush
        // Výběr minut i úklid `pending` MUSÍ být mimo setState updater: ten React
        // ve StrictMode volá dvakrát a druhý průběh by už minutu v `pending` nenašel,
        // takže by ji zahodil (chybějící svíčka i sloupec gridu, #143).
        const applied: LiveMinute[] = []
        const known = new Set(current.minutes)
        for (const ts of [...pending.keys()].sort((a, b) => (a < b ? -1 : 1))) {
          const partial = pending.get(ts)!
          // Aplikuj minutu, když má snapshot řez (nová/aktualizace mřížky) NEBO už existuje
          // (dorazil jen bar/levels/flow k uzavřené minutě — jinak by svíčka chyběla, #133).
          if (partial.rows || known.has(ts)) {
            applied.push({
              tsIso: ts,
              rows: partial.rows ?? [],
              bar: partial.bar,
              levels: partial.levels,
              flow: partial.flow,
              gexProfile: partial.gexProfile,
            })
            pending.delete(ts)
            known.add(ts)
          }
        }
        if (applied.length === 0) return
        setInputs((prev) => {
          if (!prev) return prev
          let next = prev
          for (const minute of applied) next = appendMinute(next, minute)
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
        // Starší engine pole neposílá — chybějící hodnota znamená finální bar (ADR-0005)
        final: data.final !== false,
      }
      scheduleFlush()
    }
    const onLevels = (data: ChannelData) => {
      part(minuteKey(data.ts_min)).levels = {
        flip: numOrNull(data.flip),
        centroid: numOrNull(data.centroid),
        call_wall: numOrNull(data.call_wall),
        put_wall: numOrNull(data.put_wall),
        // Sekundární zdi (ADR-0008) — starší engine pole neposílá → null
        call_wall_2: numOrNull(data.call_wall_2),
        put_wall_2: numOrNull(data.put_wall_2),
      }
      scheduleFlush()
    }
    const onFlow = (data: ChannelData) => {
      part(minuteKey(data.ts_min)).flow = { cum_delta: Number(data.cum_delta) || 0 }
      scheduleFlush()
    }
    // Dyn GEX profil minuty (ADR-0009) — starší engine kanál neposílá
    const onGexProfile = (data: ChannelData) => {
      if (!Array.isArray(data.values)) return
      part(minuteKey(data.ts_min)).gexProfile = {
        grid_start: Number(data.grid_start),
        grid_step: Number(data.grid_step),
        values: (data.values as unknown[]).map(Number),
      }
      scheduleFlush()
    }
    // Živý spot (#128): svíčky odvozené ze spotu — jen overlay, bez přepočtu gridu.
    // Minuty se drží i po uzavření, dokud pro ně nedorazí skutečný bar (#143).
    const onSpot = (data: ChannelData) => {
      const price = Number(data.price)
      if (!Number.isFinite(price)) return
      const minuteIso = floorMinuteIso(data.ts)
      setSpotBars((previous) => {
        const index = previous.findIndex((spot) => spot.minuteIso === minuteIso)
        if (index === -1) {
          const fresh: SpotBar = { minuteIso, open: price, high: price, low: price, close: price }
          return [...previous, fresh].slice(-SPOT_BARS_KEEP)
        }
        const existing = previous[index]
        // Tick beze změny OHLC nesmí přerenderovat graf (spot chodí 5×/s)
        if (existing.close === price && price <= existing.high && price >= existing.low) {
          return previous
        }
        const next = previous.slice()
        next[index] = {
          ...existing,
          high: Math.max(existing.high, price),
          low: Math.min(existing.low, price),
          close: price,
        }
        return next
      })
    }

    const snapshotCh = `snapshot.${symbol}.${expiry}`
    const priceCh = `price.${symbol}`
    const levelsCh = `levels.${symbol}.${expiry}`
    const flowCh = `flow.${symbol}`
    const spotCh = `spot.${symbol}`
    const gexProfileCh = `gexprofile.${symbol}.${expiry}`
    socket.subscribe(snapshotCh, onSnapshot)
    socket.subscribe(priceCh, onPrice)
    socket.subscribe(levelsCh, onLevels)
    socket.subscribe(flowCh, onFlow)
    socket.subscribe(spotCh, onSpot)
    socket.subscribe(gexProfileCh, onGexProfile)
    // Reconnect: dofetchni celý balík (mohli jsme zmeškat minuty)
    const offReconnect = socket.onReconnect(() => setReconcileTick((n) => n + 1))
    return () => {
      if (flushTimer !== null) clearTimeout(flushTimer)
      socket.unsubscribe(snapshotCh, onSnapshot)
      socket.unsubscribe(priceCh, onPrice)
      socket.unsubscribe(levelsCh, onLevels)
      socket.unsubscribe(flowCh, onFlow)
      socket.unsubscribe(spotCh, onSpot)
      socket.unsubscribe(gexProfileCh, onGexProfile)
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

  // Grid se skládá jen při změně dat (drahé). Statický den je identitou stabilní
  // napříč spot ticky — živé svíčky jdou zvlášť v `live` (#141).
  const baseReplay = useMemo(() => (inputs ? assembleReplayDay(inputs) : null), [inputs])
  const replayDay = useMemo(() => (baseReplay ? replayToDay(baseReplay) : null), [baseReplay])
  const live = useMemo(
    () => (baseReplay ? splitSpotBars(baseReplay, spotBars) : EMPTY_LIVE),
    [baseReplay, spotBars],
  )
  if (timeframe === 'daily') return { day: daily ?? fallback, live: EMPTY_LIVE }
  return { day: replayDay ?? fallback, live: replayDay ? live : EMPTY_LIVE }
}
