/** Denní dataset: /replay balík z API, fallback na demo data (API/engine neběží).

Timeframe 'daily' skládá sloupec za každý uložený den (seznam z /instruments/…/days,
každý den má vlastní expiraci — 0DTE řetěz).
*/
import { useEffect, useMemo, useState } from 'react'
import type { PanelSeries } from '../components/BottomPanels'
import { demoGrid, demoOverlays, demoPanels, demoProfile } from '../heatmap/demo'
import type { HeatmapGrid } from '../heatmap/grid'
import type { RawDay } from '../heatmap/modes'
import type { OverlayData } from '../heatmap/overlays'
import type { ProfileRow } from '../profile/bars'
import { autoSessions } from '../instrument/sessions'
import { buildDailyDay } from './daily'
import { fetchDays, fetchReplay } from './loader'
import type { ReplayDay } from './loader'

/** Daily pohled: strop stažených dnů (retence snapshotů je 14 dní, R4). */
const DAILY_MAX_DAYS = 14

/** Interval živého přenačtení intraday balíku (sladěno s minutovým cyklem enginu). */
const LIVE_REFETCH_MS = 60_000

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
  timeframe: 'intraday' | 'daily' = 'intraday',
): DayData {
  const fallback = useMemo(() => demoDay(), [])
  const [replay, setReplay] = useState<ReplayDay | null>(null)
  const [daily, setDaily] = useState<DayData | null>(null)

  // Změna instrumentu/expirace: starý dataset nesmí přežít (jiný symbol → demo/nový fetch)
  useEffect(() => {
    setReplay(null)
    setDaily(null)
  }, [symbol, expiry, date])

  const [retry, setRetry] = useState(0)
  // Živé přenačtení: tik každou minutu vynutí refetch balíku (engine dál produkuje data).
  // Bezpečné vůči pohledu — auto-fit se drží na resetKey (viz Heatmap #118), refetch ho neresetuje.
  const [liveTick, setLiveTick] = useState(0)
  useEffect(() => {
    if (timeframe !== 'intraday') return
    const id = setInterval(() => setLiveTick((n) => n + 1), LIVE_REFETCH_MS)
    return () => clearInterval(id)
  }, [timeframe])

  useEffect(() => {
    if (!expiry || timeframe !== 'intraday') return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    fetchReplay(symbol, expiry, date)
      .then((day) => {
        // Prázdný den (0 minut) nesmí přepsat poslední živý stav při přechodném výpadku
        if (!cancelled && day.grid.minutes > 0) setReplay(day)
      })
      .catch(() => {
        // Den (zatím) neexistuje — např. čerstvě přidaný ticker; zkusit znovu za 30 s
        if (!cancelled) timer = setTimeout(() => setRetry((n) => n + 1), 30_000)
      })
    return () => {
      cancelled = true
      if (timer !== null) clearTimeout(timer)
    }
  }, [symbol, expiry, date, timeframe, retry, liveTick])

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

  // Stabilní identita výsledku: bez memoizace by každý render vyrobil nový
  // objekt a efekty závislé na datech (např. cena v hlavičce) by se točily
  const replayDay = useMemo(() => (replay ? replayToDay(replay) : null), [replay])
  if (timeframe === 'daily') return daily ?? fallback
  return replayDay ?? fallback
}
