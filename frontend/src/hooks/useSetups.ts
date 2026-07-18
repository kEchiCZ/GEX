/** Hook setupů aktivního symbolu: REST fetch + přenačtení na WS event setups.*. */
import { useCallback, useEffect, useState } from 'react'
import { fetchSetups } from '../api/setups'
import type { SetupRow } from '../api/setups'
import { useAppState } from '../state/AppState'

export function useSetups(): { setups: SetupRow[]; refresh: () => void } {
  const { symbol, setupsVersion } = useAppState()
  const [setups, setSetups] = useState<SetupRow[]>([])
  const [manualVersion, setManualVersion] = useState(0)

  useEffect(() => {
    let cancelled = false
    fetchSetups(symbol)
      .then((rows) => {
        if (!cancelled) setSetups(rows)
      })
      .catch(() => {
        // API neběží — poslední známý stav zůstává
      })
    return () => {
      cancelled = true
    }
  }, [symbol, setupsVersion, manualVersion])

  const refresh = useCallback(() => setManualVersion((previous) => previous + 1), [])
  return { setups, refresh }
}
