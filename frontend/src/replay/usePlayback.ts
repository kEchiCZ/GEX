/** Playback stav (SPEC 7.3): pozice, ▶ s rychlostmi 1×/5×/20×, doraz vpravo = live. */
import { useCallback, useEffect, useRef, useState } from 'react'

export type PlaybackSpeed = 1 | 5 | 20

/** Interval kroku playbacku; za 1 tick se posune o `speed` minut. */
export const TICK_MS = 500

export interface Playback {
  position: number
  lastIndex: number
  playing: boolean
  speed: PlaybackSpeed
  /** Doraz vpravo — zobrazují se živá data (SPEC: návrat na live stream). */
  isLive: boolean
  play: () => void
  pause: () => void
  setSpeed: (speed: PlaybackSpeed) => void
  seek: (position: number) => void
  goLive: () => void
}

export function usePlayback(minuteCount: number): Playback {
  const lastIndex = Math.max(0, minuteCount - 1)
  const [position, setPosition] = useState(lastIndex)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState<PlaybackSpeed>(1)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Změna rozsahu (timeframe, nový den): live pozice zůstává live, jinak se
  // pozice mapuje proporcionálně — přepnutí 1m → 1h → 1m nesmí „ztratit den"
  const previousLastRef = useRef(lastIndex)
  useEffect(() => {
    const previousLast = previousLastRef.current
    previousLastRef.current = lastIndex
    setPosition((previous) => {
      if (previous >= previousLast) return lastIndex // byli jsme na live konci
      if (previousLast <= 0) return lastIndex
      return Math.min(lastIndex, Math.round((previous / previousLast) * lastIndex))
    })
  }, [lastIndex])

  useEffect(() => {
    if (!playing) return
    timerRef.current = setInterval(() => {
      setPosition((previous) => {
        const next = previous + speed
        if (next >= lastIndex) {
          setPlaying(false) // doraz vpravo = konec přehrávání, návrat na live
          return lastIndex
        }
        return next
      })
    }, TICK_MS)
    return () => {
      if (timerRef.current !== null) clearInterval(timerRef.current)
    }
  }, [playing, speed, lastIndex])

  const seek = useCallback(
    (value: number) => {
      setPosition(Math.min(lastIndex, Math.max(0, Math.round(value))))
    },
    [lastIndex],
  )

  return {
    position,
    lastIndex,
    playing,
    speed,
    isLive: position >= lastIndex,
    play: useCallback(() => setPlaying(true), []),
    pause: useCallback(() => setPlaying(false), []),
    setSpeed: useCallback((value: PlaybackSpeed) => setSpeed(value), []),
    seek,
    goLive: useCallback(() => {
      setPlaying(false)
      setPosition(lastIndex)
    }, [lastIndex]),
  }
}
