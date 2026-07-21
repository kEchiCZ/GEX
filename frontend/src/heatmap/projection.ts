/** Projekce heatmapy do konce seance (ADR-0006).

Zdi a flip jsou funkcí OI, které se mezi minutami mění málo — jejich tvar v čase
je proto do značné míry předvídatelný a je užitečné ho vidět dopředu. Projekce
drží POSLEDNÍ NAMĚŘENÝ sloupec konstantní až do settle: odpovídá předpokladu
„OI se do konce seance nezmění", nic víc. Žádná extrapolace trendu — cokoliv
chytřejšího by předstíralo znalost, kterou nemáme.

Projektovaná část se kreslí sníženou sytostí a odděluje ji svislý předěl, aby
graf netvrdil, že vpravo jsou naměřené hodnoty (viz `render.ts`).
*/
import { dataMinutesOf } from './grid'
import type { HeatmapGrid } from './grid'

/** Strop projekce — vzdálená expirace by jinak roztáhla osu do absurdna. */
export const PROJECTION_MAX_MINUTES = 24 * 60

/** Kolik minut zbývá od poslední naměřené minuty do settle; 0 = neprojektovat. */
export function projectionLength(
  lastMinuteIso: string | undefined,
  settle: Date | null,
  bucketMinutes = 1,
): number {
  if (!lastMinuteIso || !settle) return 0
  const last = new Date(lastMinuteIso)
  if (Number.isNaN(last.getTime())) return 0
  const remainingMinutes = (settle.getTime() - last.getTime()) / 60_000
  if (remainingMinutes <= 0) return 0
  const buckets = Math.floor(remainingMinutes / Math.max(1, bucketMinutes))
  return Math.max(0, Math.min(PROJECTION_MAX_MINUTES, buckets))
}

/** Rozšíří grid o `extra` sloupců zopakováním posledního naměřeného sloupce.

`extra <= 0` vrací původní grid (stabilní identita pro memoizaci). */
export function projectGrid(grid: HeatmapGrid, extra: number): HeatmapGrid {
  const dataMinutes = dataMinutesOf(grid)
  if (extra <= 0 || dataMinutes === 0) return grid
  const strikeCount = grid.strikes.length
  const total = dataMinutes + extra

  const extend = (layer: Float32Array | undefined): Float32Array | undefined => {
    if (!layer) return undefined
    const result = new Float32Array(total * strikeCount)
    for (let strikeIdx = 0; strikeIdx < strikeCount; strikeIdx += 1) {
      const from = strikeIdx * grid.minutes
      const to = strikeIdx * total
      // Naměřená část beze změny
      for (let minuteIdx = 0; minuteIdx < dataMinutes; minuteIdx += 1) {
        result[to + minuteIdx] = layer[from + minuteIdx]
      }
      // Projekce = poslední naměřený sloupec držený konstantní
      const held = layer[from + dataMinutes - 1]
      for (let minuteIdx = dataMinutes; minuteIdx < total; minuteIdx += 1) {
        result[to + minuteIdx] = held
      }
    }
    return result
  }

  return {
    minutes: total,
    dataMinutes,
    strikes: grid.strikes,
    layers: {
      call: extend(grid.layers.call),
      put: extend(grid.layers.put),
      signed: extend(grid.layers.signed),
    },
    // Stáří se neprojektuje — projekce není „stará data", je to předpoklad
    staleAge: grid.staleAge ? (extend(grid.staleAge) ?? null) : null,
  }
}

/** Popisky osy X pro projektované minuty (navazují na poslední naměřenou). */
export function projectionLabels(
  lastMinuteIso: string | undefined,
  extra: number,
  bucketMinutes: number,
  format: (iso: string) => string,
): string[] {
  if (!lastMinuteIso || extra <= 0) return []
  const last = new Date(lastMinuteIso)
  if (Number.isNaN(last.getTime())) return []
  return Array.from({ length: extra }, (_, index) =>
    format(new Date(last.getTime() + (index + 1) * bucketMinutes * 60_000).toISOString()),
  )
}
