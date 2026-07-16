/** Denní dataset: /replay balík z API, fallback na demo data (API/engine neběží). */
import { useEffect, useMemo, useState } from 'react'
import type { PanelSeries } from '../components/BottomPanels'
import { demoGrid, demoOverlays, demoPanels, demoProfile } from '../heatmap/demo'
import type { HeatmapGrid } from '../heatmap/grid'
import type { OverlayData } from '../heatmap/overlays'
import type { ProfileRow } from '../profile/bars'
import { fetchReplay } from './loader'
import type { ReplayDay } from './loader'

export interface DayData {
  source: 'replay' | 'demo'
  grid: HeatmapGrid
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
    overlays: day.overlays,
    panels: day.panels,
    profileByMinute: day.profileByMinute,
    demoProfileRows: null,
    spotSeries,
    minuteLabels,
  }
}

export function useDayData(symbol: string, expiry: string | null, date: string): DayData {
  const fallback = useMemo(() => demoDay(), [])
  const [replay, setReplay] = useState<ReplayDay | null>(null)

  useEffect(() => {
    if (!expiry) return
    let cancelled = false
    fetchReplay(symbol, expiry, date)
      .then((day) => {
        if (!cancelled && day.grid.minutes > 0) setReplay(day)
      })
      .catch(() => {
        // API neběží nebo den neexistuje — zůstává demo dataset
      })
    return () => {
      cancelled = true
    }
  }, [symbol, expiry, date])

  return replay ? replayToDay(replay) : fallback
}
