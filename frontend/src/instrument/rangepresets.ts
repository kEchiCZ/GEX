/** Presety rozsahů (#487) — čisté funkce nad 1m osou dne.

Jedním klikem typická okna pro range selector (#484). Časy US seance jdou
z TÉŽE tabulky jako markery seancí (`usSessionMs`, #511) — žádné duplicitní
hardcody, DST řeší zoneinfo. Globex noc nepotřebuje 18:00 ET vůbec: osa dne
UŽ JE Globex seance (session_frame #512), takže t1 = první minuta osy.

`positionIdx` = poslední minuta dat (live) / pozice playbacku (replay) —
klouzavé presety se na ni kotví; posun v live řeší volající 1×/min (AC).
*/
import { usSessionMs } from './sessions'
import type { RangeSelection } from './rangeselect'

export type RangePreset = 'open30' | 'rth' | 'globex' | 'last30' | 'flipcross'

export const RANGE_PRESETS: ReadonlyArray<{ value: RangePreset; label: string }> = [
  { value: 'open30', label: 'US open +30 min' },
  { value: 'rth', label: 'RTH' },
  { value: 'globex', label: 'Globex noc' },
  { value: 'last30', label: 'Posledních 30 min' },
  { value: 'flipcross', label: 'Od flip crossu' },
]

export interface PresetInputs {
  /** Datum seance (viewDate) — kotva pro časy US seance. */
  dateIso: string
  /** 1m osa dne (ISO minut) — osa může nést díry (#502). */
  minutesIso: string[]
  spotSeries: (number | null)[]
  /** Flip řada per 1m minutu (z levels overlay bundlu); null = flip neexistuje. */
  flipSeries: (number | null)[]
  /** Poslední minuta dat (live) / pozice playbacku (replay) — index 1m osy. */
  positionIdx: number
}

/** Index poslední minuty osy < `limitMs`; null když osa před limitem nezačala. */
function lastIndexBefore(minutesIso: string[], limitMs: number): number | null {
  let found: number | null = null
  for (let idx = 0; idx < minutesIso.length; idx += 1) {
    if (Date.parse(minutesIso[idx]) < limitMs) found = idx
    else break
  }
  return found
}

/** Poslední průchod ceny flipem ≤ pozici: změna znaménka (spot − flip).

Minuty bez spotu nebo flipu se přeskakují — porovnává se poslední PLATNÝ
pár, jinak by díra v datech vyrobila falešný cross. */
export function lastFlipCrossIndex(
  spotSeries: (number | null)[],
  flipSeries: (number | null)[],
  positionIdx: number,
): number | null {
  let previousSign: number | null = null
  let crossIdx: number | null = null
  const limit = Math.min(positionIdx, spotSeries.length - 1, flipSeries.length - 1)
  for (let idx = 0; idx <= limit; idx += 1) {
    const spot = spotSeries[idx]
    const flip = flipSeries[idx]
    if (spot === null || spot === undefined || flip === null || flip === undefined) continue
    const sign = Math.sign(spot - flip)
    if (sign === 0) continue // přesně na flipu — cross rozhodne až další strana
    if (previousSign !== null && sign !== previousSign) crossIdx = idx
    previousSign = sign
  }
  return crossIdx
}

/** Okno presetu; null = preset teď nedává smysl (disabled volba s tooltipem). */
export function presetRange(preset: RangePreset, inputs: PresetInputs): RangeSelection | null {
  const { dateIso, minutesIso, spotSeries, flipSeries, positionIdx } = inputs
  if (minutesIso.length === 0) return null
  const position = Math.max(0, Math.min(positionIdx, minutesIso.length - 1))
  const openMs = usSessionMs('open', dateIso)
  const closeMs = usSessionMs('close', dateIso)
  const positionMs = Date.parse(minutesIso[position])

  const window = (fromMs: number, toMs: number): RangeSelection | null => {
    if (toMs <= fromMs) return null
    return {
      fromIso: new Date(fromMs).toISOString(),
      toIso: new Date(Math.min(toMs, positionMs)).toISOString(),
    }
  }

  switch (preset) {
    case 'open30':
      // Před US openem nemá okno data — disabled
      if (positionMs < openMs) return null
      return window(openMs, openMs + 30 * 60_000)
    case 'rth':
      if (positionMs < openMs) return null
      return window(openMs, closeMs)
    case 'globex': {
      // Osa dne = Globex seance (#512): t1 = její první minuta, t2 = US open
      const end = Math.min(openMs, positionMs)
      const startMs = Date.parse(minutesIso[0])
      if (end <= startMs) return null
      // Konec okna: poslední minuta PŘED openem (open už patří US seanci)
      const endIdx = lastIndexBefore(minutesIso, Math.min(openMs, positionMs + 60_000))
      if (endIdx === null || endIdx === 0) return null
      return { fromIso: minutesIso[0], toIso: minutesIso[Math.min(endIdx, position)] }
    }
    case 'last30': {
      const fromIdx = Math.max(0, position - 29)
      return { fromIso: minutesIso[fromIdx], toIso: minutesIso[position] }
    }
    case 'flipcross': {
      const crossIdx = lastFlipCrossIndex(spotSeries, flipSeries, position)
      if (crossIdx === null) return null
      return { fromIso: minutesIso[crossIdx], toIso: minutesIso[position] }
    }
  }
}

/** Důvod nedostupnosti do tooltipu disabled volby. */
export function presetDisabledReason(preset: RangePreset): string {
  switch (preset) {
    case 'open30':
    case 'rth':
      return 'US seance ještě nezačala'
    case 'globex':
      return 'Overnight část seance není v datech'
    case 'flipcross':
      return 'Cena dnes flipem neprošla'
    case 'last30':
      return 'Bez dat dne'
  }
}
