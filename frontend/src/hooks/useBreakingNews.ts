/** Data karty Breaking news (E-6.28c): `GET /news/breaking` à 15 s a po WS zprávě.

Dopad „běží X min“ se mění po minutách, nová zpráva ale musí naskočit hned —
proto vedle periodického fetchu i refetch po WS `news` s významnou zprávou
(klasifikace do ~1 s od příjmu, #1496). `enabled = false` (sbalený panel)
neposílá nic.
*/
import { useEffect, useState } from 'react'
import { fetchBreaking } from '../api/breaking'
import type { BreakingCard } from '../api/breaking'
import { useAppState } from '../state/AppState'

export const BREAKING_REFRESH_MS = 15_000
/** Dávka klasifikace dorazí jako několik WS zpráv za sebou — jeden refetch stačí. */
const PUSH_DEBOUNCE_MS = 500

export interface BreakingState {
  card: BreakingCard | null
  error: string | null
}

export function useBreakingNews(enabled: boolean): BreakingState {
  const { socket } = useAppState()
  const [card, setCard] = useState<BreakingCard | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [version, setVersion] = useState(0)

  // Po sbalení nic nedržet — po rozbalení by se na chvíli ukázal starý
  // „běží 2 min“, jako by byl aktuální
  useEffect(() => {
    if (!enabled) return
    return () => {
      setCard(null)
      setError(null)
    }
  }, [enabled])

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    const load = () => {
      fetchBreaking().then(
        (next) => {
          if (cancelled) return
          setCard(next)
          setError(null)
        },
        (reason: unknown) => {
          if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason))
        },
      )
    }
    load()
    const timer = window.setInterval(load, BREAKING_REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [enabled, version])

  useEffect(() => {
    if (!enabled) return
    let pending: number | undefined
    const handler = (data: Record<string, unknown>) => {
      // Syrová páska z enginu ještě nemá klasifikaci; kartu změní až významná
      if (typeof data.id !== 'number' || typeof data.significance !== 'number') return
      window.clearTimeout(pending)
      pending = window.setTimeout(() => setVersion((value) => value + 1), PUSH_DEBOUNCE_MS)
    }
    socket.subscribe('news', handler)
    return () => {
      window.clearTimeout(pending)
      socket.unsubscribe('news', handler)
    }
  }, [socket, enabled])

  return { card, error }
}
