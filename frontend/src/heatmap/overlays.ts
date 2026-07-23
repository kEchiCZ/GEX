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
  name: string // flip | call_wall | put_wall | centroid | max_pain | walls:*
  color: string
  series: (number | null)[] // hodnota (strike) per minuta
  /** Vzor čárkování (canvas setLineDash); bez něj plná čára. */
  dash?: number[]
  /** Slabá zeď per minuta (dominance < WALL_DOM_WEAK, ADR-0010, #223);
      true = úsek se kreslí ztlumeně, null = dominance neznámá (plný styl). */
  weak?: (boolean | null)[]
  /** Přípona cenovky úrovně (aktuální dominance zdi, např. " · 34 %"). */
  labelSuffix?: string
}

/** Práh slabé zdi (ADR-0010, #223) — pod ním se linie kreslí ztlumeně.
    Zrcadlí engine default `GEXLENS_SETUP_MIN_WALL_DOMINANCE`. */
export const WALL_DOM_WEAK = 0.15

/** Cenovka úrovně: zaokrouhlení na 2 desetinná místa bez koncových nul. */
export function formatLevel(value: number): string {
  return String(Math.round(value * 100) / 100)
}

/** Poslední ne-null hodnota řady — horizontální projekce úrovně (Moodix styl). */
export function lastLevelValue(series: (number | null)[]): number | null {
  for (let index = series.length - 1; index >= 0; index -= 1) {
    const value = series[index]
    if (value !== null && value !== undefined) return value
  }
  return null
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

/** Dvě linie zdí spárované PO ÚROVNÍCH, ne po pořadí síly (ADR-0008, #92). */
export interface PairedWallSeries {
  upper: (number | null)[]
  lower: (number | null)[]
  /** Na které linii leží poslední známá PRIMÁRNÍ zeď (plný styl + cenovka). */
  primaryIsUpper: boolean
}

/** Spáruje primární a sekundární zeď do dvou úrovňově stabilních linií.

Primární zeď (argmax koncentrace) mezi dvěma rovnocennými úrovněmi minutu po
minutě přeskakuje — kreslit ji přímo dává svislé pruhy (#92). Párování po
úrovních drží každou linii na její hladině: v minutě se dvěma kandidáty jde
vyšší strike na `upper`, nižší na `lower`; jediný kandidát se přiřadí linii
s bližší poslední hodnotou (řada bez sekundární zdi tak zůstává úrovňově
stabilní, i když primární přeskakuje). */
export function pairWallSeries(
  primary: (number | null)[],
  secondary: (number | null)[],
): PairedWallSeries {
  const upper: (number | null)[] = []
  const lower: (number | null)[] = []
  let lastUpper: number | null = null
  let lastLower: number | null = null
  for (let t = 0; t < primary.length; t += 1) {
    const p = primary[t] ?? null
    const s = secondary[t] ?? null
    if (p !== null && s !== null) {
      const hi = Math.max(p, s)
      const lo = Math.min(p, s)
      upper.push(hi)
      lower.push(lo)
      lastUpper = hi
      lastLower = lo
    } else if (p !== null || s !== null) {
      const value = (p ?? s)!
      const upperGap = lastUpper === null ? Number.POSITIVE_INFINITY : Math.abs(value - lastUpper)
      const lowerGap = lastLower === null ? Number.POSITIVE_INFINITY : Math.abs(value - lastLower)
      if (upperGap <= lowerGap) {
        upper.push(value)
        lower.push(null)
        lastUpper = value
      } else {
        upper.push(null)
        lower.push(value)
        lastLower = value
      }
    } else {
      upper.push(null)
      lower.push(null)
    }
  }
  let primaryIsUpper = true
  for (let t = primary.length - 1; t >= 0; t -= 1) {
    const p = primary[t]
    if (p !== null && p !== undefined) {
      primaryIsUpper = upper[t] === p
      break
    }
  }
  return { upper, lower, primaryIsUpper }
}

/** Nahradí pár (zeď, sekundární zeď) úrovňově spárovanými liniemi (ADR-0008).

Vypnuto (`enabled=false`): sekundární linie se zahodí a primární se kreslí
jako dřív. Zapnuto: primární styl (barva + cenovka) dostane linie, na které
primární zeď AKTUÁLNĚ leží; druhá linie je tečkovaná bez cenovky (název
s prefixem `walls:` — kreslí se jen jako řada). */
export function resolveSecondaryWalls(walls: LevelLine[], enabled: boolean): LevelLine[] {
  const result: LevelLine[] = []
  for (const line of walls) {
    if (line.name.endsWith('_2')) continue // sekundární se zpracuje u primární
    const secondary = walls.find((item) => item.name === `${line.name}_2`)
    const hasSecondary =
      enabled && secondary !== undefined && secondary.series.some((value) => value !== null)
    if (!hasSecondary) {
      result.push(line)
      continue
    }
    const paired = pairWallSeries(line.series, secondary.series)
    const primarySeries = paired.primaryIsUpper ? paired.upper : paired.lower
    const altSeries = paired.primaryIsUpper ? paired.lower : paired.upper
    // Párování míchá hodnoty primární a sekundární zdi po úrovních — per-minutové
    // weak flagy by po prohození patřily jiné zdi, proto se zahazují (ADR-0010)
    result.push({ ...line, series: primarySeries, weak: undefined })
    result.push({
      ...secondary,
      name: `walls:${secondary.name}`,
      series: altSeries,
      weak: undefined,
    })
  }
  return result
}

/** Práh skoku úrovně v STRIKE KROCÍCH — větší skok linii přeruší (#197). */
export const LEVEL_JUMP_MAX_STEPS = 10

/** Skok úrovně mezi sousedními minutami větší než práh (#197).

Flip může mít víc skutečných nulových průchodů a mezi nimi přeskakovat —
svislá spojnice přes celý graf je vizuální šum, mezera je čitelnější. */
export function isLevelJump(previous: number, current: number, strikeStep: number): boolean {
  return strikeStep > 0 && Math.abs(current - previous) > LEVEL_JUMP_MAX_STEPS * strikeStep
}

/** Linie, které se při velkém skoku přerušují (flip; zdi řeší párování ADR-0008). */
export function breaksOnJump(name: string): boolean {
  return name === 'flip' || name === 'walls:flip'
}

/** Indexy pro popisky osy: každý k-tý tak, aby rozestup na obrazovce byl ≥ minSpacingPx. */
export function tickIndices(count: number, stepPx: number, minSpacingPx: number): number[] {
  if (count <= 0 || stepPx <= 0) return []
  const every = Math.max(1, Math.ceil(minSpacingPx / stepPx))
  const indices: number[] = []
  for (let index = 0; index < count; index += every) {
    indices.push(index)
  }
  return indices
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
