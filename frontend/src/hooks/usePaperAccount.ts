/** Stav paper účtu (#1187): REST poll à 10 s + ruční refresh po akci.

Fily dělá engine po minutě, takže poll stačí; po podání/zavření orderu
se volá `refresh`, ať chip nečeká na další tik. */
import { useCallback, useEffect, useState } from 'react'
import { fetchPaperAccount } from '../api/paper'
import type { PaperAccount } from '../api/paper'

const POLL_MS = 10_000

export function usePaperAccount(): { account: PaperAccount | null; refresh: () => void } {
  const [account, setAccount] = useState<PaperAccount | null>(null)
  const [version, setVersion] = useState(0)

  useEffect(() => {
    let cancelled = false
    const load = () => {
      void fetchPaperAccount().then((result) => {
        if (!cancelled && result !== null) setAccount(result)
      })
    }
    load()
    const timer = window.setInterval(load, POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [version])

  const refresh = useCallback(() => setVersion((previous) => previous + 1), [])
  return { account, refresh }
}
