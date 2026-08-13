/** Referenční úrovně (#678): fetch barů dnešní + předchozí seance à 60 s.

Jen když je vrstva zapnutá (Traders mode + intraday) — jinak se nefetchuje
nic. Bary jdou z lehkého GET /bars (#674), předchozí seance z uloženého
seznamu dnů (previousStoredDay).
*/
import { useEffect, useState } from 'react'
import { fetchBars, fetchStoredDays, previousStoredDay, usOpenMs } from '../api/briefing'
import { computeReferenceLevels } from '../instrument/referencelevels'
import type { ReferenceLevels } from '../instrument/referencelevels'

const REFRESH_MS = 60_000

export function useReferenceLevels(
  symbol: string,
  dateIso: string,
  enabled: boolean,
): ReferenceLevels | null {
  const [levels, setLevels] = useState<ReferenceLevels | null>(null)

  useEffect(() => {
    if (!enabled) {
      setLevels(null)
      return
    }
    let cancelled = false
    const load = async () => {
      const [todayBars, days] = await Promise.all([
        fetchBars(symbol, dateIso),
        fetchStoredDays(symbol),
      ])
      const previous = previousStoredDay(days, dateIso)
      const prevDayBars = previous ? await fetchBars(symbol, previous) : []
      if (cancelled) return
      setLevels(
        computeReferenceLevels({
          todayBars,
          prevDayBars,
          usOpenMs: usOpenMs(dateIso),
          nowMs: Date.now(),
        }),
      )
    }
    void load()
    const timer = window.setInterval(() => void load(), REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [symbol, dateIso, enabled])

  return levels
}
