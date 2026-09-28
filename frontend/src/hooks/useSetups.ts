/** Hook setupů aktivního symbolu: REST fetch + přenačtení na WS event setups.*. */
import { useCallback, useEffect, useState } from 'react'
import { fetchSetups } from '../api/setups'
import type { SetupRow } from '../api/setups'
import { useAppState } from '../state/AppState'

export function useSetups(): {
  setups: SetupRow[]
  /** Počet všech setupů symbolu (stránka má strop 200, #1319); null = neznámý. */
  totalCount: number | null
  refresh: () => void
} {
  const { symbol, setupsVersion } = useAppState()
  const [setups, setSetups] = useState<SetupRow[]>([])
  const [totalCount, setTotalCount] = useState<number | null>(null)
  const [manualVersion, setManualVersion] = useState(0)

  useEffect(() => {
    let cancelled = false
    fetchSetups(symbol)
      .then((page) => {
        if (cancelled) return
        setSetups(page.setups)
        setTotalCount(page.totalCount)
      })
      .catch(() => {
        // API neběží — poslední známý stav zůstává
      })
    return () => {
      cancelled = true
    }
  }, [symbol, setupsVersion, manualVersion])

  const refresh = useCallback(() => setManualVersion((previous) => previous + 1), [])
  return { setups, totalCount, refresh }
}
