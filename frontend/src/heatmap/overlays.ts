/** Overlaye heatmapy (SPEC 7.2): cenová křivka, sessions, levels, walls — čisté helpery. */

export interface PriceBar {
  minuteIdx: number
  close: number
  /** Směr ticku vůči předchozí minutě (barva úseku křivky). */
  up: boolean
  /** OHLC pro svíčkový režim (volitelné — bez nich se bar kreslí jen v křivce). */
  open?: number
  high?: number
  low?: number
}

/** Styl vykreslení ceny nad heatmapou. */
export type PriceStyle = 'line' | 'candles'

export interface SessionMarker {
  minuteIdx: number
  label: string
}

export interface LevelLine {
  name: string // flip | call_wall | put_wall | centroid
  color: string
  series: (number | null)[] // hodnota (strike) per minuta
}

export interface OverlayData {
  price?: PriceBar[]
  sessions?: SessionMarker[]
  levels?: LevelLine[]
  walls?: LevelLine[]
  /** Timestamp posledních dat (zobrazený v rohu). */
  timestamp?: string
}

export interface OverlayToggles {
  gexLevels: boolean
  sessions: boolean
  dynGex: boolean
}

/** Overlay přepínače odpovídají checkboxům (AC issue #24) — filtr viditelných vrstev. */
export function visibleOverlays(data: OverlayData, toggles: OverlayToggles): OverlayData {
  return {
    price: data.price, // cenová křivka je vždy viditelná (SPEC 7.2)
    timestamp: data.timestamp,
    sessions: toggles.sessions ? data.sessions : undefined,
    levels: toggles.gexLevels ? data.levels : undefined,
    walls: toggles.dynGex ? data.walls : undefined,
  }
}

/** Interpolovaná řádková pozice hodnoty (strike/cena) v ose strikes; index 0 = nejnižší. */
export function fractionalRow(strikes: number[], value: number): number | null {
  if (strikes.length === 0) return null
  if (value <= strikes[0]) return 0
  const last = strikes.length - 1
  if (value >= strikes[last]) return last
  for (let index = 0; index < last; index += 1) {
    const low = strikes[index]
    const high = strikes[index + 1]
    if (value >= low && value <= high) {
      return index + (value - low) / (high - low)
    }
  }
  return null
}

export interface PolylinePoint {
  minuteIdx: number
  row: number
  up: boolean
}

/** Cenová křivka → body v souřadnicích buněk (řádek interpolovaný mezi strikes). */
export function pricePolyline(bars: PriceBar[], strikes: number[]): PolylinePoint[] {
  const points: PolylinePoint[] = []
  for (const bar of bars) {
    const row = fractionalRow(strikes, bar.close)
    if (row !== null) {
      points.push({ minuteIdx: bar.minuteIdx, row, up: bar.up })
    }
  }
  return points
}

export interface CandleGeometry {
  minuteIdx: number
  openRow: number
  closeRow: number
  highRow: number
  lowRow: number
  /** Zelená/červená svíčka podle open vs. close. */
  up: boolean
}

/** Svíčky → řádkové souřadnice; bary bez kompletního OHLC se přeskakují. */
export function candleGeometry(bars: PriceBar[], strikes: number[]): CandleGeometry[] {
  const candles: CandleGeometry[] = []
  for (const bar of bars) {
    if (bar.open === undefined || bar.high === undefined || bar.low === undefined) continue
    const openRow = fractionalRow(strikes, bar.open)
    const closeRow = fractionalRow(strikes, bar.close)
    const highRow = fractionalRow(strikes, bar.high)
    const lowRow = fractionalRow(strikes, bar.low)
    if (openRow === null || closeRow === null || highRow === null || lowRow === null) continue
    candles.push({
      minuteIdx: bar.minuteIdx,
      openRow,
      closeRow,
      highRow,
      lowRow,
      up: bar.close >= bar.open,
    })
  }
  return candles
}
