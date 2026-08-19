/** Lazy dotahování historických seancí (#788) — jeden den na požádání.

Kráčí od včerejška zpátky; 404 = den bez seance (víkend, svátek) a přeskakuje
se, po `MISS_LIMIT` dírách v řadě se považuje archiv za vyčerpaný (start
archivu barů, 28. 7. 2024). Vždy nejvýš jeden request v letu — scroll může
`requestMore` volat, jak chce, dotáhne se přesně jeden další den.
*/

import { useCallback, useEffect, useRef, useState } from 'react'

import { fetchHistoryDay, previousDateIso } from '../replay/history'
import type { HistoryDay } from '../replay/history'

/** Kolik prázdných dnů v řadě znamená konec archivu — nejdelší reálná díra
je prodloužený víkend (3–4 dny), pět v řadě už je začátek historie. */
const MISS_LIMIT = 5

export interface HistoryBarsState {
  /** Od nejnovějšího (včerejšek) po nejstarší načtený. */
  days: HistoryDay[]
  /** Archiv došel — další `requestMore` už nic neudělá. */
  exhausted: boolean
  requestMore: () => void
}

export function useHistoryBars(symbol: string, todayIso: string): HistoryBarsState {
  const [days, setDays] = useState<HistoryDay[]>([])
  const [exhausted, setExhausted] = useState(false)
  // Kurzor a zámky v ref — requestMore se volá z kreslicí cesty a nesmí
  // měnit identitu podle průběžného stavu (renderovací smyčka, #141)
  const cursorRef = useRef(todayIso)
  const loadingRef = useRef(false)
  const exhaustedRef = useRef(false)

  useEffect(() => {
    // Nový instrument (nebo den) = čistý start; staré dny nepatří k nové ose
    cursorRef.current = todayIso
    loadingRef.current = false
    exhaustedRef.current = false
    setDays([])
    setExhausted(false)
  }, [symbol, todayIso])

  const requestMore = useCallback(() => {
    if (loadingRef.current || exhaustedRef.current) return
    loadingRef.current = true
    const run = async () => {
      let misses = 0
      let cursor = cursorRef.current
      while (misses < MISS_LIMIT) {
        cursor = previousDateIso(cursor)
        const day = await fetchHistoryDay(symbol, cursor)
        cursorRef.current = cursor
        if (day !== null) {
          setDays((previous) => [...previous, day])
          return
        }
        misses += 1
      }
      exhaustedRef.current = true
      setExhausted(true)
    }
    run()
      .catch(() => {
        // Síťová chyba není konec archivu — příští scroll to zkusí znovu
      })
      .finally(() => {
        loadingRef.current = false
      })
  }, [symbol])

  return { days, exhausted, requestMore }
}
