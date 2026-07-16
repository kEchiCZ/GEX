/** Datový model heatmapy: matice čas × strike per vrstva (SPEC 7.2). */

export interface HeatmapLayers {
  call?: Float32Array
  put?: Float32Array
  signed?: Float32Array
}

export interface HeatmapGrid {
  /** Počet minut (šířka, osa X). */
  minutes: number
  /** Strikes vzestupně (výška, osa Y — index 0 = nejnižší strike). */
  strikes: number[]
  /** Hodnoty vrstev normalizované na 0..1 (signed −1..1); index = strikeIdx * minutes + minuteIdx. */
  layers: HeatmapLayers
  /** Stáří dat buňky v sekundách (stale > STALE_THRESHOLD_S se kreslí odlišně), nebo null. */
  staleAge: Float32Array | null
}

export function cellIndex(
  grid: Pick<HeatmapGrid, 'minutes'>,
  strikeIdx: number,
  minuteIdx: number,
): number {
  return strikeIdx * grid.minutes + minuteIdx
}

export interface GridCell {
  minuteIdx: number
  strike: number
  layer: keyof HeatmapLayers
  value: number
  staleAge?: number
}

/** Postaví grid z buněk (chybějící buňky = 0). Strikes se odvodí a seřadí. */
export function buildGrid(minutes: number, cells: GridCell[]): HeatmapGrid {
  const strikes = [...new Set(cells.map((cell) => cell.strike))].sort((a, b) => a - b)
  const strikeIdx = new Map(strikes.map((strike, index) => [strike, index]))
  const size = minutes * strikes.length
  const layers: HeatmapLayers = {}
  let staleAge: Float32Array | null = null

  for (const cell of cells) {
    let layer = layers[cell.layer]
    if (!layer) {
      layer = new Float32Array(size)
      layers[cell.layer] = layer
    }
    const index = (strikeIdx.get(cell.strike) ?? 0) * minutes + cell.minuteIdx
    layer[index] = cell.value
    if (cell.staleAge !== undefined) {
      staleAge ??= new Float32Array(size)
      staleAge[index] = cell.staleAge
    }
  }
  return { minutes, strikes, layers, staleAge }
}
