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

/** Řada do pozice t se zachovanou délkou (stabilní osa X): po t nuly. */
export function sliceSeries<T extends number | null>(values: T[], position: number): T[] {
  return values.map((value, index) => (index <= position ? value : (0 as T)))
}

/** Řada s dírami do pozice t se zachovanou délkou: po t null (linie končí, nepropadne k nule). */
function sliceLine(series: (number | null)[], position: number): (number | null)[] {
  return series.map((value, index) => (index <= position ? value : null))
}

/** Overlaye do pozice t: cena useknutá, levels/walls po t = null (linie končí). */
export function sliceOverlays(full: OverlayData, position: number): OverlayData {
  return {
    ...full,
    price: full.price?.filter((bar) => bar.minuteIdx <= position),
    levels: full.levels?.map((line) => ({ ...line, series: sliceLine(line.series, position) })),
    walls: full.walls?.map((line) => ({ ...line, series: sliceLine(line.series, position) })),
    sessions: full.sessions?.filter((session) => session.minuteIdx <= position),
    // Signály vzniklé po pozici jsou v přetáčeném čase budoucnost (#295);
    // stopa platnosti se nechává celá — expirace byla známá už při vzniku
    signals: full.signals?.filter((signal) => signal.minuteIdx <= position),
  }
}

export function slicePanels(full: PanelSeries, position: number): PanelSeries {
  return {
    vol: sliceSeries(full.vol, position),
    optVolCall: sliceSeries(full.optVolCall, position),
    optVolPut: sliceSeries(full.optVolPut, position),
    // Cum Δ s mezerami (ADR-0057 bod 3): po pozici null — plocha končí na
    // pozici, nula by po ní kreslila vyrovnaný tok
    cumDelta: sliceLine(full.cumDelta, position),
    // Zdroj znaménka a počet minut bez tisků po pozici neznáme (look-ahead):
    // false / 0 — přetáčení nesmí prozradit budoucí mezeru ani důvod skrytí
    cumDeltaMidpoint:
      full.cumDeltaMidpoint && full.cumDeltaMidpoint.map((value, index) => index <= position && value), // prettier-ignore
    cumDeltaGapMinutes:
      full.cumDeltaGapMinutes && full.cumDeltaGapMinutes.map((value, index) => (index <= position ? value : 0)), // prettier-ignore
    // Ruční výčet tu dřív vynechal CVD podkladu i Evo OI — v playbacku obojí
    // zmizelo (#1578); úplnost hlídá test se sadou klíčů PanelSeries
    futuresCvd: full.futuresCvd && sliceLine(full.futuresCvd, position),
    deltaFlowCall: sliceSeries(full.deltaFlowCall, position),
    deltaFlowPut: sliceSeries(full.deltaFlowPut, position),
    // Evo OI je úroveň: po pozici null, schod končí na pozici — nula by
    // tvrdila „pozice zmizely" (#1591)
    evoOiCall: full.evoOiCall && sliceLine(full.evoOiCall, position),
    evoOiPut: full.evoOiPut && sliceLine(full.evoOiPut, position),
    // Popisek „od HH:MM" (#518) je vlastnost dne, ne pozice — přetáčením nemizí
    cumDeltaFromIso: full.cumDeltaFromIso,
  }
}
