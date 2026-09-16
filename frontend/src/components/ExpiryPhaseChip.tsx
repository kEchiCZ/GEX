/** Chip ⌛ v hlavičce (#1189): fáze kvartálního expiračního týdne — roll, OPEX
týden, den expirace (SOQ), týden po. Mimo tyhle fáze se nekreslí. Data z
GET /calendar/expiry, refresh à 30 min (fáze se mění jen s dnem). */
import { useEffect, useState } from 'react'
import { fetchExpiryCalendar, phaseChipLabel, phaseTooltip } from '../api/calendar'
import type { ExpiryCalendar } from '../api/calendar'

const REFRESH_MS = 30 * 60_000

/** Sdílené načtení kalendáře — chip v hlavičce, ⌛ v grafu (App) i Briefing. */
export function useExpiryCalendar(): ExpiryCalendar | null {
  const [calendar, setCalendar] = useState<ExpiryCalendar | null>(null)
  useEffect(() => {
    let cancelled = false
    const load = () => {
      void fetchExpiryCalendar().then((result) => {
        if (!cancelled) setCalendar(result)
      })
    }
    load()
    const timer = window.setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [])
  return calendar
}

export function ExpiryPhaseChip({ calendar }: { calendar: ExpiryCalendar | null }) {
  if (calendar === null) return null
  const label = phaseChipLabel(calendar)
  if (label === null) return null
  return (
    <span
      className={`muted expiry-phase-chip phase-${calendar.phase}`}
      data-testid="expiry-phase-chip"
      title={phaseTooltip(calendar)}
    >
      ⌛ {label}
    </span>
  )
}
