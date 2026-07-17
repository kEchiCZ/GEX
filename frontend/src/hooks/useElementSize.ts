/** Sledování skutečné velikosti elementu (ResizeObserver) pro hi-DPI canvas.

Canvas s pevným rozlišením roztažený přes CSS se rozmazává na velkých
monitorech — rozlišení musí sledovat zobrazenou velikost × devicePixelRatio.
V jsdom (testy) ResizeObserver není a rozměry jsou nulové → drží se výchozí
velikost, takže souřadnice v testech zůstávají deterministické.
*/
import { useEffect, useRef, useState } from 'react'

export interface ElementSize {
  width: number
  height: number
}

export function useElementSize<T extends HTMLElement>(
  fallback: ElementSize,
): { ref: React.RefObject<T | null>; size: ElementSize } {
  const ref = useRef<T | null>(null)
  const [size, setSize] = useState<ElementSize>(fallback)

  useEffect(() => {
    const element = ref.current
    if (!element || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect
      if (!rect || rect.width <= 0 || rect.height <= 0) return
      setSize((previous) => {
        const width = Math.round(rect.width)
        const height = Math.round(rect.height)
        return previous.width === width && previous.height === height ? previous : { width, height }
      })
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  return { ref, size }
}
