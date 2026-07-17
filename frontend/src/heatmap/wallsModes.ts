/** Walls módy (SPEC 4.4) — TS zrcadlo engine compute/walls.py: Peak, Center,
Smooth (EMA span 15), Flip (řada z levels), Ridge (lokální maxima s prominence
filtrem spojená nejbližším strikem). Čisté funkce nad vrstvou gridu.
*/

export const WALLS_MODES = [
  { value: 'off', label: 'Off' },
  { value: 'peak', label: 'Peak' },
  { value: 'center', label: 'Center' },
  { value: 'smooth', label: 'Smooth' },
  { value: 'flip', label: 'Flip' },
  { value: 'ridge', label: 'Ridge' },
] as const
export type WallsMode = (typeof WALLS_MODES)[number]['value']

function columnValue(layer: Float32Array, minutes: number, strikeIdx: number, t: number): number {
  return layer[strikeIdx * minutes + t]
}

/** Peak: argmax metriky per minuta; null pro nulový sloupec. */
export function peakSeries(
  layer: Float32Array,
  minutes: number,
  strikes: number[],
): (number | null)[] {
  return Array.from({ length: minutes }, (_, t) => {
    let best = -Infinity
    let bestStrike: number | null = null
    for (let strikeIdx = 0; strikeIdx < strikes.length; strikeIdx += 1) {
      const value = columnValue(layer, minutes, strikeIdx, t)
      if (value > best) {
        best = value
        bestStrike = strikes[strikeIdx]
      }
    }
    return best > 0 ? bestStrike : null
  })
}

/** Center: vážené těžiště (|hodnota| jako váha) per minuta. */
export function centerSeries(
  layer: Float32Array,
  minutes: number,
  strikes: number[],
): (number | null)[] {
  return Array.from({ length: minutes }, (_, t) => {
    let total = 0
    let weighted = 0
    for (let strikeIdx = 0; strikeIdx < strikes.length; strikeIdx += 1) {
      const weight = Math.abs(columnValue(layer, minutes, strikeIdx, t))
      total += weight
      weighted += strikes[strikeIdx] * weight
    }
    return total === 0 ? null : weighted / total
  })
}

/** Smooth: EMA řady (None mezery drží poslední EMA stav). */
export function smoothSeries(values: (number | null)[], span = 15): (number | null)[] {
  const alpha = 2 / (span + 1)
  let ema: number | null = null
  return values.map((value) => {
    if (value !== null) {
      ema = ema === null ? value : alpha * value + (1 - alpha) * ema
    }
    return ema
  })
}

/** Prominence vrcholu: výška nad nejvyšším sedlem směrem k vyššímu vrcholu. */
function prominence(values: number[], index: number): number {
  const height = values[index]
  const saddles: number[] = []
  for (const step of [-1, 1]) {
    let lowest = height
    let saddle: number | null = null
    for (let i = index + step; i >= 0 && i < values.length; i += step) {
      lowest = Math.min(lowest, values[i])
      if (values[i] > height) {
        saddle = lowest
        break
      }
    }
    if (saddle !== null) saddles.push(saddle)
  }
  if (saddles.length === 0) return height // globální maximum
  return height - Math.max(...saddles)
}

/** Lokální maxima sloupce s relativním prominence filtrem (šum netvoří hřeben). */
export function localMaxima(values: number[], strikes: number[], prominenceRatio = 0.1): number[] {
  const globalMax = Math.max(...values, 0)
  if (globalMax <= 0) return []
  const threshold = prominenceRatio * globalMax
  const maxima: number[] = []
  for (let i = 0; i < values.length; i += 1) {
    const left = i > 0 ? values[i - 1] : -Infinity
    const right = i < values.length - 1 ? values[i + 1] : -Infinity
    if (values[i] <= left || values[i] <= right) continue
    if (prominence(values, i) >= threshold) maxima.push(strikes[i])
  }
  return maxima
}

export interface RidgePoint {
  minuteIdx: number
  strike: number
}

/** Ridge: maxima spojená mezi sousedními minutami nejbližším strikem do hřebenů. */
export function ridgeTracks(
  layer: Float32Array,
  minutes: number,
  strikes: number[],
  prominenceRatio = 0.1,
): RidgePoint[][] {
  const tracks: RidgePoint[][] = []
  let openTracks: RidgePoint[][] = []
  for (let t = 0; t < minutes; t += 1) {
    const values = strikes.map((_, strikeIdx) => columnValue(layer, minutes, strikeIdx, t))
    const maxima = localMaxima(values, strikes, prominenceRatio)
    const candidates: Array<{ gap: number; strike: number; track: RidgePoint[] }> = []
    for (const strike of maxima) {
      for (const track of openTracks) {
        const last = track[track.length - 1]
        if (last.minuteIdx === t - 1) {
          candidates.push({ gap: Math.abs(last.strike - strike), strike, track })
        }
      }
    }
    candidates.sort((a, b) => a.gap - b.gap)
    const matchedStrikes = new Set<number>()
    const matchedTracks = new Set<RidgePoint[]>()
    for (const { strike, track } of candidates) {
      if (matchedStrikes.has(strike) || matchedTracks.has(track)) continue
      track.push({ minuteIdx: t, strike })
      matchedStrikes.add(strike)
      matchedTracks.add(track)
    }
    for (const strike of maxima) {
      if (!matchedStrikes.has(strike)) {
        const track: RidgePoint[] = [{ minuteIdx: t, strike }]
        tracks.push(track)
        openTracks.push(track)
      }
    }
    openTracks = openTracks.filter((track) => track[track.length - 1].minuteIdx === t)
  }
  return tracks
}
