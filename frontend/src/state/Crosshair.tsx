/** Crosshair sdílený napříč panely (SPEC 7.2): heatmapa, strike profil, spodní panely.

Dvě cesty k témuž údaji (#492):

- **Bus** — imperativní kanál (mutable pole + listeners) ve stabilním contextu.
  Heatmapa přes něj čte i píše VÝHRADNĚ: mousemove tak nevyvolá žádný její
  React commit; dynamická canvas vrstva se překreslí z odběru busu.
- **State** — `useCrosshair()` pro panely a profil. Plní se z busu s dedupe
  per buňka (minuteIdx + strike): pohyb uvnitř jedné buňky negeneruje commit,
  přechod mezi buňkami commitne jen lehké konzumenty (SVG linky, odečty).
*/
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'

export interface CrosshairPosition {
  minuteIdx: number
  /** null = pozice přišla z panelu, který zná jen časovou osu (spodní panely). */
  strike: number | null
}

/** Surová pozice kurzoru v CSS px plochy heatmapy (spojitá cena na ose Y). */
export interface CrosshairPointer {
  x: number
  y: number
}

type BusListener = () => void

export interface CrosshairBus {
  /** Aktuální pozice — mutable pole, čti až v okamžiku kreslení. */
  readonly position: CrosshairPosition | null
  readonly pointer: CrosshairPointer | null
  set(position: CrosshairPosition | null, pointer: CrosshairPointer | null): void
  /** Vrací unsubscribe; listener běží synchronně v rámci set(). */
  subscribe(listener: BusListener): () => void
}

class CrosshairBusImpl implements CrosshairBus {
  position: CrosshairPosition | null = null
  pointer: CrosshairPointer | null = null
  private listeners = new Set<BusListener>()

  set(position: CrosshairPosition | null, pointer: CrosshairPointer | null): void {
    this.position = position
    this.pointer = pointer
    for (const listener of this.listeners) listener()
  }

  subscribe(listener: BusListener): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }
}

interface CrosshairState {
  position: CrosshairPosition | null
  setPosition: (position: CrosshairPosition | null) => void
}

const CrosshairContext = createContext<CrosshairState | null>(null)
const CrosshairBusContext = createContext<CrosshairBus | null>(null)

function sameCell(a: CrosshairPosition | null, b: CrosshairPosition | null): boolean {
  if (a === null || b === null) return a === b
  return a.minuteIdx === b.minuteIdx && a.strike === b.strike
}

export function CrosshairProvider({ children }: { children: ReactNode }) {
  const busRef = useRef<CrosshairBusImpl | null>(null)
  busRef.current ??= new CrosshairBusImpl()
  const bus = busRef.current
  const [position, setPositionState] = useState<CrosshairPosition | null>(null)

  // Zrcadlení bus → state s dedupe per buňka: panely dostanou commit jen
  // při přechodu mezi buňkami, ne na každý pixel
  useEffect(
    () =>
      bus.subscribe(() => {
        const next = bus.position
        setPositionState((previous) => (sameCell(previous, next) ? previous : next))
      }),
    [bus],
  )

  // Zápis z panelů (znají jen buňku, ne pixel) jde toutéž cestou přes bus,
  // aby heatmapa dostala notifikaci i o pozici nastavené jinde
  const setPosition = useCallback((next: CrosshairPosition | null) => bus.set(next, null), [bus])
  const value = useMemo(() => ({ position, setPosition }), [position, setPosition])
  return (
    <CrosshairBusContext.Provider value={bus}>
      <CrosshairContext.Provider value={value}>{children}</CrosshairContext.Provider>
    </CrosshairBusContext.Provider>
  )
}

export function useCrosshair(): CrosshairState {
  const state = useContext(CrosshairContext)
  if (state === null) {
    throw new Error('useCrosshair musí být uvnitř CrosshairProvider')
  }
  return state
}

/** Imperativní kanál pro hot path (#492) — odběr NEvyvolává re-render. */
export function useCrosshairBus(): CrosshairBus {
  const bus = useContext(CrosshairBusContext)
  if (bus === null) {
    throw new Error('useCrosshairBus musí být uvnitř CrosshairProvider')
  }
  return bus
}
