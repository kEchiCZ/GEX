/** Krájení denních dat na pozici playbacku (SPEC 7.3) — čisté funkce v paměti. */
import type { PanelSeries } from '../components/BottomPanels'
import type { HeatmapGrid } from '../heatmap/grid'
import type { OverlayData } from '../heatmap/overlays'

/** Grid do pozice t: buňky po t jsou prázdné (heatmapa se „přetáčí"). */
export function sliceGrid(full: HeatmapGrid, position: number): HeatmapGrid {
  const cut = Math.min(position, full.minutes - 1)
  const sliceLayer = (layer?: Float32Array): Float32Array | undefined => {
    if (!layer) return undefined
    const copy = new Float32Array(layer.length)
    for (let strikeIdx = 0; strikeIdx < full.strikes.length; strikeIdx += 1) {
      const offset = strikeIdx * full.minutes
      copy.set(layer.subarray(offset, offset + cut + 1), offset)
    }
    return copy
  }
  return {
    minutes: full.minutes,
    strikes: full.strikes,
    layers: {
      call: sliceLayer(full.layers.call),
      put: sliceLayer(full.layers.put),
      signed: sliceLayer(full.layers.signed),
    },
    staleAge: full.staleAge ? sliceLayer(full.staleAge)! : null,
  }
}

/** Řada do pozice t s zachovanou délkou (stabilní osa X): po t nuly. */
export function sliceSeries(values: number[], position: number): number[] {
  return values.map((value, index) => (index <= position ? value : 0))
}

/** Overlaye do pozice t: cena useknutá, levels/walls po t = null (linie končí). */
export function sliceOverlays(full: OverlayData, position: number): OverlayData {
  const cutLine = (series: (number | null)[]): (number | null)[] =>
    series.map((value, index) => (index <= position ? value : null))
  return {
    ...full,
    price: full.price?.filter((bar) => bar.minuteIdx <= position),
    levels: full.levels?.map((line) => ({ ...line, series: cutLine(line.series) })),
    walls: full.walls?.map((line) => ({ ...line, series: cutLine(line.series) })),
    sessions: full.sessions?.filter((session) => session.minuteIdx <= position),
  }
}

export function slicePanels(full: PanelSeries, position: number): PanelSeries {
  return {
    vol: sliceSeries(full.vol, position),
    optVolCall: sliceSeries(full.optVolCall, position),
    optVolPut: sliceSeries(full.optVolPut, position),
    cumDelta: sliceSeries(full.cumDelta, position),
    deltaFlowCall: sliceSeries(full.deltaFlowCall, position),
    deltaFlowPut: sliceSeries(full.deltaFlowPut, position),
  }
}
