/** Sledování skutečné velikosti elementu pro hi-DPI canvas a sdílené osy.

Canvas s pevným rozlišením roztažený přes CSS se rozmazává a rozjíždí měřítka
panelů — rozlišení musí sledovat zobrazenou velikost × devicePixelRatio.

Primárně ResizeObserver; navíc přeměření po mountu, na window resize a lehký
interval fallback — RO notifikace se může ztratit (headless/virtual-time,
„loop limit" prohlížeče) a zamrzlé měřítko rozhodí celý graf. V jsdom (testy)
jsou rozměry nulové → drží se výchozí velikost a souřadnice testů zůstávají
deterministické.
*/
import { useEffect, useRef, useState } from 'react'

export interface ElementSize {
  width: number
  height: number
}

const FALLBACK_POLL_MS = 1000

export function useElementSize<T extends HTMLElement>(
  fallback: ElementSize,
): { ref: React.RefObject<T | null>; size: ElementSize } {
  const ref = useRef<T | null>(null)
  const [size, setSize] = useState<ElementSize>(fallback)

  useEffect(() => {
    const element = ref.current
    if (!element) return

    const measure = () => {
      const rect = element.getBoundingClientRect()
      if (rect.width <= 0 || rect.height <= 0) return
      const width = Math.round(rect.width)
      const height = Math.round(rect.height)
      setSize((previous) =>
        previous.width === width && previous.height === height ? previous : { width, height },
      )
    }

    measure()
    let observer: ResizeObserver | undefined
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(measure)
      observer.observe(element)
    }
    const interval = setInterval(measure, FALLBACK_POLL_MS)
    window.addEventListener('resize', measure)
    return () => {
      observer?.disconnect()
      clearInterval(interval)
      window.removeEventListener('resize', measure)
    }
  }, [])

  return { ref, size }
}
