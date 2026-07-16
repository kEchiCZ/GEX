/** Barevné rampy heatmapy: call zelená/teal, put červená, signed divergentní (SPEC 7.2). */

export type Rgba = [number, number, number, number]

/** Stáří dat, od kterého je buňka vizuálně stale (SPEC kap. 8: > 5 min). */
export const STALE_THRESHOLD_S = 300

function clamp01(value: number): number {
  return value < 0 ? 0 : value > 1 ? 1 : value
}

/** Call vrstva: teal/zelená, intenzita = hodnota. */
export function callColor(value: number): Rgba {
  const v = clamp01(value)
  return [Math.round(20 * v), Math.round(200 * v), Math.round(170 * v), Math.round(v * 255)]
}

/** Put vrstva: červená, intenzita = hodnota. */
export function putColor(value: number): Rgba {
  const v = clamp01(value)
  return [Math.round(230 * v), Math.round(45 * v), Math.round(60 * v), Math.round(v * 255)]
}

/** Signed vrstva (−1..1): záporné červeně, kladné zeleně. */
export function signedColor(value: number): Rgba {
  const v = value < -1 ? -1 : value > 1 ? 1 : value
  if (v >= 0) return [24, Math.round(190 * v), Math.round(160 * v), Math.round(v * 255)]
  return [Math.round(230 * -v), Math.round(45 * -v), Math.round(60 * -v), Math.round(-v * 255)]
}

/** Alpha kompozice `over` přes `base` (call + put vrstvy do jednoho pixelu). */
export function blend(base: Rgba, over: Rgba): Rgba {
  const alphaOver = over[3] / 255
  const alphaBase = (base[3] / 255) * (1 - alphaOver)
  const alpha = alphaOver + alphaBase
  if (alpha === 0) return [0, 0, 0, 0]
  const mix = (index: 0 | 1 | 2) =>
    Math.round((over[index] * alphaOver + base[index] * alphaBase) / alpha)
  return [mix(0), mix(1), mix(2), Math.round(alpha * 255)]
}

/** Stale buňka: desaturace k šedé + snížená alfa — data jsou stará, ne prázdná. */
export function applyStale(color: Rgba): Rgba {
  const gray = Math.round(0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2])
  const mixToGray = (channel: number) => Math.round(channel * 0.35 + gray * 0.65)
  return [
    mixToGray(color[0]),
    mixToGray(color[1]),
    mixToGray(color[2]),
    Math.round(color[3] * 0.55),
  ]
}
