/** Hook konturových segmentů (#493): worker + multi-slot cache + sync fallback.

- Výpočet (blur + marching squares, při All až 7 úrovní nad ~260k buňkami)
  běží ve web workeru — datový update 1×/min už nedropne frame na main threadu.
- Cache je WeakMap per zdrojové pole × mód: přepínání Off↔Major↔All nad
  stejnými daty je cache hit bez výpočtu (AC 2); invalidace přirozeně novým
  gridem (nový Float32Array od loaderu).
- Bez `Worker` (jsdom testy, SSR) běží synchronní fallback — týž kód, jen na
  main threadu; chování testů beze změny.
- Během asynchronního výpočtu se drží PŘEDCHOZÍ segmenty — kontury nebliknou.
*/
import { useEffect, useState } from 'react'
import type { ContoursMode } from './contours'
import { computeContourPolylines } from './contourCompute'
import { decodePolylines } from './polylines'
import type { Polyline } from './polylines'
import type { HeatmapGrid } from './grid'

const EMPTY: Polyline[] = []

//: Cache per zdrojové pole (WeakMap → uvolní se s gridem) × mód
const cache = new WeakMap<Float32Array, Map<ContoursMode, Polyline[]>>()

let worker: Worker | null = null
let workerFailed = false
let nextRequestId = 1
const pending = new Map<number, (flat: Float32Array) => void>()

function getWorker(): Worker | null {
  if (workerFailed || typeof Worker === 'undefined') return null
  if (worker === null) {
    try {
      worker = new Worker(new URL('./contours.worker.ts', import.meta.url), { type: 'module' })
      worker.addEventListener(
        'message',
        (event: MessageEvent<{ id: number; buffer: ArrayBuffer }>) => {
          const resolve = pending.get(event.data.id)
          if (resolve) {
            pending.delete(event.data.id)
            resolve(new Float32Array(event.data.buffer))
          }
        },
      )
      worker.addEventListener('error', () => {
        // Worker se nepodařilo spustit (CSP, build) — trvalý sync fallback,
        // ať se chyba neopakuje při každém updatu
        workerFailed = true
        worker = null
        for (const [, resolve] of pending) resolve(new Float32Array(0))
        pending.clear()
      })
    } catch {
      workerFailed = true
      worker = null
    }
  }
  return worker
}

function sourceField(
  grid: HeatmapGrid,
  underGrid: HeatmapGrid | null | undefined,
): {
  field: Float32Array | null
  width: number
  height: number
} {
  // S Dyn GEX podkladem (#242) obrysují kontury modelované pole;
  // podklad má z App zaručené shodné rozměry s hlavním gridem
  const source =
    underGrid &&
    underGrid.minutes === grid.minutes &&
    underGrid.strikes.length === grid.strikes.length
      ? underGrid
      : grid
  const field = source.layers.signed ?? source.layers.call ?? source.layers.put ?? null
  return { field, width: source.minutes, height: source.strikes.length }
}

function workerAvailable(): boolean {
  return !workerFailed && typeof Worker !== 'undefined'
}

function storeSegments(field: Float32Array, mode: ContoursMode, segments: Polyline[]): void {
  const perMode = cache.get(field) ?? new Map<ContoursMode, Polyline[]>()
  perMode.set(mode, segments)
  cache.set(field, perMode)
}

export function useContours(
  grid: HeatmapGrid,
  underGrid: HeatmapGrid | null | undefined,
  mode: ContoursMode,
): Polyline[] {
  const { field, width, height } = sourceField(grid, underGrid)
  // Poslední segmenty doručené workerem — během asynchronního výpočtu se
  // drží (kontury nebliknou). Stav místo ref čteného při renderu (#1123).
  const [delivered, setDelivered] = useState<Polyline[]>(EMPTY)

  let cached = field ? cache.get(field)?.get(mode) : undefined
  if (!cached && mode !== 'off' && field && !workerAvailable()) {
    // Sync fallback přímo v renderu: výsledek jde do cache per pole × mód,
    // takže je to memoizace (další render ji čte), ne vedlejší efekt
    cached = computeContourPolylines(field, width, height, mode)
    storeSegments(field, mode, cached)
  }

  useEffect(() => {
    if (mode === 'off' || !field || cached) return
    const target = getWorker()
    if (target === null) {
      // Konstruktor workeru právě selhal (CSP, build): jednorázově spočítat
      // synchronně, další rendery už jdou sync fallbackem výše
      const segments = computeContourPolylines(field, width, height, mode)
      storeSegments(field, mode, segments)
      // eslint-disable-next-line react-hooks/set-state-in-effect -- jediná cesta k překreslení po selhání workeru
      setDelivered(segments)
      return
    }
    let cancelled = false
    const id = nextRequestId
    nextRequestId += 1
    pending.set(id, (flat) => {
      if (cancelled) return
      const segments = decodePolylines(flat)
      storeSegments(field, mode, segments)
      setDelivered(segments)
    })
    // Kopie pole: originál drží render heatmapy, transfer by ho odpojil
    const copy = Float32Array.from(field)
    target.postMessage({ id, buffer: copy.buffer, width, height, mode }, [copy.buffer])
    return () => {
      cancelled = true
      pending.delete(id)
    }
  }, [field, width, height, mode, cached])

  if (mode === 'off' || !field) return EMPTY
  // Výpočet běží — držet předchozí doručené segmenty
  return cached ?? delivered
}
