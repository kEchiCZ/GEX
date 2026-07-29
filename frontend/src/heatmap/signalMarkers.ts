/** Šipky signálů na cenové křivce (#295, SPEC 9.0) — čisté funkce.

Signály se mapují na minutu grafu stejně jako news markery: **podle popisku
minuty, ne podle pořadí**, týmž formatterem, který vyrobil popisky osy.
Stopa platnosti vede od šipky do `expiry_ts`; expirace za koncem osy se
ořízne na poslední minutu (signál platí „až do konce zobrazeného dne").
*/
import { categoryLabel } from '../api/news'
import type { SignalRow } from '../api/news'

export interface SignalMarker {
  minuteIdx: number
  /** Konec vodorovné stopy platnosti (index osy, clamp na konec). */
  endIdx: number
  direction: 'long' | 'short'
  /** 0–1; řídí sytost šipky (SPEC 9.0). */
  strength: number
  mode: 'NEWS' | 'COMBINED'
  /** Signál ještě platí → kandidát na varovný badge při unconfirmed změně. */
  active: boolean
  /** Varovný badge ⚠ — nepotvrzená intradenní změna stavu (SPEC 6.3). */
  warning: boolean
  /** Řádky tooltipu: režim, zdůvodnění, n vzorků, Wilson LB. */
  tooltip: string
}

/** Číslo z `inputs` JSONu; chybějící/nevalidní → null. */
function inputNumber(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

/** Tooltip signálu (SPEC 9.0): režim, zdůvodnění, n, Wilson LB. */
export function signalTooltip(signal: SignalRow): string {
  const inputs = signal.inputs ?? {}
  const bucket = (inputs.bucket ?? {}) as Record<string, unknown>
  const parts: string[] = [
    `${signal.direction === 'long' ? '▲ Long' : '▼ Short'} · ${signal.mode}`,
    `síla ${signal.strength.toFixed(2)}`,
  ]
  const category = typeof inputs.category === 'string' ? inputs.category : null
  const importance = inputNumber(inputs.importance)
  if (category) {
    parts.push(`${categoryLabel(category)}${importance !== null ? ` imp ${importance}` : ''}`)
  }
  const n = inputNumber(bucket.n)
  const lb = inputNumber(bucket.hit_rate_lb)
  if (n !== null) parts.push(`n=${n}`)
  if (lb !== null) parts.push(`LB ${(lb * 100).toFixed(0)} %`)
  return parts.join(' · ')
}

export function buildSignalMarkers(
  signals: SignalRow[],
  labels: string[],
  formatLabel: (iso: string) => string,
  options: { now?: Date; warning?: boolean } = {},
): SignalMarker[] {
  if (labels.length === 0) return []
  const now = options.now ?? new Date()
  const indexByLabel = new Map<string, number>()
  labels.forEach((label, index) => {
    if (!indexByLabel.has(label)) indexByLabel.set(label, index)
  })

  const markers: SignalMarker[] = []
  for (const signal of signals) {
    const minuteIdx = indexByLabel.get(formatLabel(signal.ts))
    if (minuteIdx === undefined) continue // mimo zobrazený den
    // Expirace mimo osu = platnost přesahuje zobrazený den → stopa do konce
    const endIdx = indexByLabel.get(formatLabel(signal.expiry_ts)) ?? labels.length - 1
    const active = new Date(signal.expiry_ts).getTime() > now.getTime()
    markers.push({
      minuteIdx,
      endIdx: Math.max(minuteIdx, endIdx),
      direction: signal.direction,
      strength: Math.min(1, Math.max(0, signal.strength)),
      mode: signal.mode,
      active,
      warning: active && (options.warning ?? false),
      tooltip: signalTooltip(signal),
    })
  }
  return markers.sort((a, b) => a.minuteIdx - b.minuteIdx)
}

/** Barva šipky: teal long / červená short; sytost dle strength (SPEC 9.0). */
export function signalColor(marker: SignalMarker, alphaScale = 1): string {
  const alpha = (0.35 + 0.6 * marker.strength) * alphaScale
  return marker.direction === 'long' ? `rgba(20,184,166,${alpha})` : `rgba(224,82,96,${alpha})`
}

/** Signál na dané minutě (tooltip u crosshairu); null = žádný. */
export function signalAt(markers: SignalMarker[], minuteIdx: number | null): SignalMarker | null {
  if (minuteIdx === null) return null
  return markers.find((marker) => marker.minuteIdx === minuteIdx) ?? null
}
