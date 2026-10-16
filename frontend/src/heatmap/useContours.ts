/** Hook konturových segmentů (#493): worker + multi-slot cache + sync fallback.

- Výpočet (blur + marching squares, při All až 7 úrovní nad ~260k buňkami)
  běží ve web workeru — datový update 1×/min už nedropne frame na main threadu.
- Cache je WeakMap per zdrojové pole × mód: přepínání Off↔Major↔All nad
  stejnými daty je cache hit bez výpočtu (AC 2); invalidace přirozeně novým
  gridem (nový Float32Array od loaderu).
- Hladiny i flip (#1174) chybějící v cache jdou jednou úlohou workeru nad
  sdíleným blurem a přijdou jedním doručením, takže statická vrstva se po
  konturách kreslí jednou (#1420). Úloha odchází v layout efektu, ještě před
  kresbou statické vrstvy, aby worker počítal souběžně s ní.
- Bez `Worker` (jsdom testy, SSR) běží synchronní fallback — týž kód, jen na
  main threadu; chování testů beze změny.
- Během asynchronního výpočtu se drží PŘEDCHOZÍ segmenty — kontury nebliknou.
*/
import { useLayoutEffect, useState } from 'react'
import { splitContoursMode } from './contours'
import type { ContoursMode } from './contours'
import { computeContourPolylinesForModes } from './contourCompute'
import { decodePolylines } from './polylines'
import type { Polyline } from './polylines'
import type { HeatmapGrid } from './grid'

/** Kontury pole: hladiny (Major/All) a nulová izolinie flipu, každé jiným stylem. */
export interface ContourSet {
  levels: Polyline[]
  flip: Polyline[]
}

const EMPTY: Polyline[] = []
const NO_CONTOURS: ContourSet = { levels: EMPTY, flip: EMPTY }

//: Cache per zdrojové pole (WeakMap → uvolní se s gridem) × mód
const cache = new WeakMap<Float32Array, Map<ContoursMode, Polyline[]>>()

let worker: Worker | null = null
let workerFailed = false
let nextRequestId = 1
const pending = new Map<number, (flats: Float32Array[]) => void>()

function getWorker(): Worker | null {
  if (workerFailed || typeof Worker === 'undefined') return null
  if (worker === null) {
    try {
      worker = new Worker(new URL('./contours.worker.ts', import.meta.url), { type: 'module' })
      worker.addEventListener(
        'message',
        (event: MessageEvent<{ id: number; buffers: ArrayBuffer[] }>) => {
          const resolve = pending.get(event.data.id)
          if (resolve) {
            pending.delete(event.data.id)
            resolve(event.data.buffers.map((buffer) => new Float32Array(buffer)))
          }
        },
      )
      worker.addEventListener('error', () => {
        // Worker se nepodařilo spustit (CSP, build) — trvalý sync fallback,
        // ať se chyba neopakuje při každém updatu
        workerFailed = true
        worker = null
        for (const [, resolve] of pending) resolve([])
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

/** Segmenty módu z cache; vypnutý mód nebo pole bez dat = prázdno, chybějící = undefined. */
function cachedSegments(field: Float32Array | null, mode: ContoursMode): Polyline[] | undefined {
  if (mode === 'off' || field === null) return EMPTY
  return cache.get(field)?.get(mode)
}

/** Módy kontur (hladiny, flip), které aktuální volba potřebuje. */
function contourModes(contours: ContoursMode): { levels: ContoursMode; flip: ContoursMode } {
  const split = splitContoursMode(contours)
  return { levels: split.levels, flip: split.flip ? 'flip' : 'off' }
}

/** Spočte módy synchronně (fallback bez workeru) a uloží je do cache. */
function computeIntoCache(
  field: Float32Array,
  width: number,
  height: number,
  modes: ContoursMode[],
): void {
  const sets = computeContourPolylinesForModes(field, width, height, modes)
  modes.forEach((mode, index) => storeSegments(field, mode, sets[index]))
}

export function useContours(
  grid: HeatmapGrid,
  underGrid: HeatmapGrid | null | undefined,
  contours: ContoursMode,
): ContourSet {
  const { field, width, height } = sourceField(grid, underGrid)
  const modes = contourModes(contours)
  // Poslední kontury doručené workerem — během asynchronního výpočtu se
  // drží (kontury nebliknou). Stav místo ref čteného při renderu (#1123).
  const [delivered, setDelivered] = useState<ContourSet>(NO_CONTOURS)

  const missingOf = () => [modes.levels, modes.flip].filter((mode) => !cachedSegments(field, mode))
  if (field && !workerAvailable() && missingOf().length > 0) {
    // Sync fallback přímo v renderu: výsledek jde do cache per pole × mód,
    // takže je to memoizace (další render ji čte), ne vedlejší efekt
    computeIntoCache(field, width, height, missingOf())
  }
  const levels = cachedSegments(field, modes.levels)
  const flip = cachedSegments(field, modes.flip)
  // Primitivní klíč chybějících módů: efekt se nespouští při každém renderu
  const missingKey = [levels ? '' : modes.levels, flip ? '' : modes.flip].filter(Boolean).join(',')

  useLayoutEffect(() => {
    if (!field || missingKey === '') return
    const missing = missingKey.split(',') as ContoursMode[]
    const settle = () =>
      setDelivered({
        levels: cachedSegments(field, modes.levels) ?? EMPTY,
        flip: cachedSegments(field, modes.flip) ?? EMPTY,
      })
    const target = getWorker()
    if (target === null) {
      // Konstruktor workeru právě selhal (CSP, build): jednorázově spočítat
      // synchronně, další rendery už jdou sync fallbackem výše
      computeIntoCache(field, width, height, missing)
      settle() // jediná cesta k překreslení po selhání workeru

      return
    }
    let cancelled = false
    const id = nextRequestId
    nextRequestId += 1
    pending.set(id, (flats) => {
      if (cancelled) return
      missing.forEach((mode, index) =>
        storeSegments(field, mode, decodePolylines(flats[index] ?? new Float32Array(0))),
      )
      settle()
    })
    // Kopie pole: originál drží render heatmapy, transfer by ho odpojil
    const copy = Float32Array.from(field)
    target.postMessage({ id, buffer: copy.buffer, width, height, modes: missing }, [copy.buffer])
    return () => {
      cancelled = true
      pending.delete(id)
    }
  }, [field, width, height, missingKey, modes.levels, modes.flip])

  // Výpočet běží — držet předchozí doručené kontury
  return {
    levels: levels ?? delivered.levels,
    flip: flip ?? delivered.flip,
  }
}

/** Jsou kontury volby pro aktuální pole spočtené (hladiny i flip)? Měřič výkonu
(#1409) podle toho ukončí span Mode/Scale až vykreslením kontur nové mřížky;
do té doby `useContours` drží předchozí kontury. */
export function contoursSettled(
  grid: HeatmapGrid,
  underGrid: HeatmapGrid | null | undefined,
  contours: ContoursMode,
): boolean {
  const { field } = sourceField(grid, underGrid)
  const modes = contourModes(contours)
  return (
    cachedSegments(field, modes.levels) !== undefined &&
    cachedSegments(field, modes.flip) !== undefined
  )
}
