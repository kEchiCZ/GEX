/** Crosshair sdílený napříč panely (SPEC 7.2): heatmapa, strike profil, spodní panely. */
import { createContext, useContext, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

export interface CrosshairPosition {
  minuteIdx: number
  /** null = pozice přišla z panelu, který zná jen časovou osu (spodní panely). */
  strike: number | null
}

interface CrosshairState {
  position: CrosshairPosition | null
  setPosition: (position: CrosshairPosition | null) => void
}

const CrosshairContext = createContext<CrosshairState | null>(null)

export function CrosshairProvider({ children }: { children: ReactNode }) {
  const [position, setPosition] = useState<CrosshairPosition | null>(null)
  const value = useMemo(() => ({ position, setPosition }), [position])
  return <CrosshairContext.Provider value={value}>{children}</CrosshairContext.Provider>
}

export function useCrosshair(): CrosshairState {
  const state = useContext(CrosshairContext)
  if (state === null) {
    throw new Error('useCrosshair musí být uvnitř CrosshairProvider')
  }
  return state
}
