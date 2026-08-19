/** Lazy dotahování historických seancí (#788) — jeden den na požádání.

Kráčí od včerejška zpátky; 404 = den bez seance (víkend, svátek) a přeskakuje
se, po `MISS_LIMIT` dírách v řadě se považuje archiv za vyčerpaný (start
archivu barů, 28. 7. 2024). Vždy nejvýš jeden request v letu — scroll může
`requestMore` volat, jak chce, dotáhne se přesně jeden další den.

Kurzor se NEODVOZUJE ze samostatného ref, ale z data posledního načteného dne
— první verze držela kurzor zvlášť a resetovací efekt (rodič běží AŽ PO
efektech dítěte) ho na mountu vrátil na dnešek pod rukama prvnímu běhu:
tentýž den se pak stahoval dokola a dál se nikdy nedošlo. Odvozený kurzor
tuhle třídu chyb vylučuje a duplicitní den nemá jak vzniknout.
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
  // Zrcadlo v ref: requestMore se volá z kreslicí cesty a nesmí měnit
  // identitu podle průběžného stavu (renderovací smyčka, #141)
  const daysRef = useRef<HistoryDay[]>([])
  const loadingRef = useRef(false)
  const exhaustedRef = useRef(false)
  // Generace běhu: reset (změna instrumentu/dne) zneplatní odpověď v letu,
  // aby den staré osy nedopadl do nové
  const generationRef = useRef(0)
  const keyRef = useRef(`${symbol}|${todayIso}`)

  useEffect(() => {
    const key = `${symbol}|${todayIso}`
    if (keyRef.current === key) return // mount — počáteční hodnoty už platí
    keyRef.current = key
    generationRef.current += 1
    daysRef.current = []
    loadingRef.current = false
    exhaustedRef.current = false
    setDays([])
    setExhausted(false)
  }, [symbol, todayIso])

  const requestMore = useCallback(() => {
    if (loadingRef.current || exhaustedRef.current) return
    loadingRef.current = true
    const generation = generationRef.current
    const run = async () => {
      let misses = 0
      let cursor = daysRef.current[daysRef.current.length - 1]?.date ?? todayIso
      while (misses < MISS_LIMIT) {
        cursor = previousDateIso(cursor)
        const day = await fetchHistoryDay(symbol, cursor)
        if (generation !== generationRef.current) return // mezitím reset — zahodit
        if (day !== null) {
          daysRef.current = [...daysRef.current, day]
          setDays(daysRef.current)
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
  }, [symbol, todayIso])

  return { days, exhausted, requestMore }
}
