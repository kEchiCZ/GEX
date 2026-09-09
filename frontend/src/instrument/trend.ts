/** Trend napříč timeframy (#1089, karta Trend v Briefingu).

Čtení shora dolů, jak to dělají diskreční tradeři: vyšší timeframe (W, D)
určuje směr, nižší (4h, 1h, 15m) načasování. Per timeframe se kombinují dvě
nezávislé věci:

- **Struktura trhu** — fraktálové pivoty (swing high = svíčka s nejvyšším high
  v okně ±n, swing low obdobně). Poslední dva swing highs i lows rostou
  = HH/HL = rostoucí; klesají = LH/LL = klesající; jinak bez trendu.
- **Klouzavé průměry** — EMA20 a EMA50: cena vs. EMA20, EMA20 vs. EMA50, sklon
  EMA20 za posledních 5 svíček.

Verdikt timeframe je silný, když struktura i EMA souhlasí; slabý, když směr
říká jen jedna z nich. Bez dostatku svíček se nic nedosazuje — řádek říká
„málo dat" (stejný princip jako ADR-0028).

Čisté funkce bez React závislostí — testují se samostatně. */

export interface Candle {
  ts: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  /** Rozdělaná svíčka (API): struktura ji nepoužívá, EMA ano (průběžný stav). */
  partial?: boolean
}

export type TrendDirection = 'up' | 'down' | 'range'

export const TIMEFRAMES = ['W', 'D', '240', '60', '15'] as const
export type TimeframeKey = (typeof TIMEFRAMES)[number]

export const TIMEFRAME_LABELS: Record<TimeframeKey, string> = {
  W: 'Týden',
  D: 'Den',
  '240': '4h',
  '60': '1h',
  '15': '15m',
}

/** Vyšší timeframy určují směr, nižší načasování. */
export const HIGHER_TIMEFRAMES: readonly TimeframeKey[] = ['W', 'D']
export const LOWER_TIMEFRAMES: readonly TimeframeKey[] = ['240', '60', '15']

/** Šířka fraktálu: D/W mají méně svíček, užší okno; intradenní širší proti šumu. */
const PIVOT_WIDTH: Record<TimeframeKey, number> = { W: 2, D: 3, '240': 3, '60': 3, '15': 3 }

/** Minimum svíček pro verdikt — EMA20 potřebuje aspoň své okno. */
export const MIN_CANDLES = 20
const EMA_FAST = 20
const EMA_SLOW = 50
const SLOPE_LOOKBACK = 5

export interface Pivot {
  index: number
  price: number
  kind: 'high' | 'low'
}

export type StructureKind = 'HH/HL' | 'LH/LL' | 'mixed'

export interface StructureReading {
  kind: StructureKind
  lastHigh: number | null
  lastLow: number | null
}

export interface EmaReading {
  ema20: number
  /** Null, když je svíček méně než 50. */
  ema50: number | null
  priceAboveFast: boolean
  /** Null bez EMA50. */
  fastAboveSlow: boolean | null
  /** Sklon EMA20 za posledních 5 svíček v bodech. */
  slope: number
  direction: TrendDirection
}

export interface TimeframeTrend {
  tf: TimeframeKey
  candles: number
  direction: TrendDirection | null
  strength: 'strong' | 'weak' | null
  structure: StructureReading | null
  ema: EmaReading | null
  /** Krátké vysvětlení pro tabulku; „málo dat" bez verdiktu. */
  note: string
}

export interface TrendReport {
  byTimeframe: TimeframeTrend[]
  /** Směr vyšších TF (W, D); null = ani jeden nemá verdikt. */
  higher: TrendDirection | null
  /** Směr nižších TF (4h, 1h, 15m) — většina; null bez verdiktu. */
  lower: TrendDirection | null
  /** Kolik TF s verdiktem souhlasí se směrem vyšších TF / kolik jich verdikt má. */
  aligned: number
  decided: number
  /** Čtení pro den jednou až dvěma větami. */
  reading: string
  /** Očekávaný směr z trendu pro verdikt dne (#1090): null = bez převahy. */
  expected: TrendDirection | null
}

/** Exponenciální klouzavý průměr; první hodnota = prostý průměr prvních `period`. */
export function ema(values: number[], period: number): number[] {
  if (values.length < period) return []
  const k = 2 / (period + 1)
  const seed = values.slice(0, period).reduce((sum, value) => sum + value, 0) / period
  const result: number[] = [seed]
  for (let i = period; i < values.length; i += 1) {
    const previous = result[result.length - 1]
    result.push(values[i] * k + previous * (1 - k))
  }
  return result
}

/** Fraktálové pivoty: high vyšší než `width` sousedů na obě strany (low obdobně). */
export function findPivots(candles: Candle[], width: number): Pivot[] {
  const pivots: Pivot[] = []
  for (let i = width; i < candles.length - width; i += 1) {
    let isHigh = true
    let isLow = true
    for (let offset = 1; offset <= width; offset += 1) {
      const left = candles[i - offset]
      const right = candles[i + offset]
      if (candles[i].high <= left.high || candles[i].high <= right.high) isHigh = false
      if (candles[i].low >= left.low || candles[i].low >= right.low) isLow = false
      if (!isHigh && !isLow) break
    }
    if (isHigh) pivots.push({ index: i, price: candles[i].high, kind: 'high' })
    if (isLow) pivots.push({ index: i, price: candles[i].low, kind: 'low' })
  }
  return pivots
}

/** Struktura z posledních dvou swing highs a lows; null bez dvou od každého. */
export function readStructure(pivots: Pivot[]): StructureReading | null {
  const highs = pivots.filter((pivot) => pivot.kind === 'high')
  const lows = pivots.filter((pivot) => pivot.kind === 'low')
  if (highs.length < 2 || lows.length < 2) return null
  const [prevHigh, lastHigh] = highs.slice(-2)
  const [prevLow, lastLow] = lows.slice(-2)
  const higherHighs = lastHigh.price > prevHigh.price
  const higherLows = lastLow.price > prevLow.price
  const lowerHighs = lastHigh.price < prevHigh.price
  const lowerLows = lastLow.price < prevLow.price
  let kind: StructureKind = 'mixed'
  if (higherHighs && higherLows) kind = 'HH/HL'
  else if (lowerHighs && lowerLows) kind = 'LH/LL'
  return { kind, lastHigh: lastHigh.price, lastLow: lastLow.price }
}

/** EMA čtení z close; null pod MIN_CANDLES. */
export function readEma(candles: Candle[]): EmaReading | null {
  if (candles.length < MIN_CANDLES) return null
  const closes = candles.map((candle) => candle.close)
  const fast = ema(closes, EMA_FAST)
  const slow = closes.length >= EMA_SLOW ? ema(closes, EMA_SLOW) : []
  const ema20 = fast[fast.length - 1]
  const ema50 = slow.length > 0 ? slow[slow.length - 1] : null
  const lookback = Math.min(SLOPE_LOOKBACK, fast.length - 1)
  const slope = ema20 - fast[fast.length - 1 - lookback]
  const price = closes[closes.length - 1]
  const priceAboveFast = price > ema20
  const fastAboveSlow = ema50 === null ? null : ema20 > ema50
  let direction: TrendDirection = 'range'
  if (priceAboveFast && slope > 0 && fastAboveSlow !== false) direction = 'up'
  else if (!priceAboveFast && slope < 0 && fastAboveSlow !== true) direction = 'down'
  return { ema20, ema50, priceAboveFast, fastAboveSlow, slope, direction }
}

function structureDirection(structure: StructureReading | null): TrendDirection | null {
  if (structure === null) return null
  if (structure.kind === 'HH/HL') return 'up'
  if (structure.kind === 'LH/LL') return 'down'
  return 'range'
}

const DIRECTION_LABELS: Record<TrendDirection, string> = {
  up: 'rostoucí',
  down: 'klesající',
  range: 'bez trendu',
}

export function directionLabel(direction: TrendDirection | null): string {
  return direction === null ? '—' : DIRECTION_LABELS[direction]
}

/** Verdikt jednoho timeframe. Struktura se čte jen z uzavřených svíček. */
export function assessTimeframe(tf: TimeframeKey, candles: Candle[]): TimeframeTrend {
  const closed = candles.filter((candle) => !candle.partial)
  if (candles.length < MIN_CANDLES) {
    return {
      tf,
      candles: candles.length,
      direction: null,
      strength: null,
      structure: null,
      ema: null,
      note: `málo dat (${candles.length}/${MIN_CANDLES} svíček)`,
    }
  }
  const structure = readStructure(findPivots(closed, PIVOT_WIDTH[tf]))
  const emaReading = readEma(candles)
  const fromStructure = structureDirection(structure)
  const fromEma = emaReading?.direction ?? null
  let direction: TrendDirection | null = null
  let strength: 'strong' | 'weak' | null = null
  if (fromStructure !== null && fromStructure !== 'range' && fromStructure === fromEma) {
    direction = fromStructure
    strength = 'strong'
  } else if (fromStructure !== null && fromStructure !== 'range' && fromEma === 'range') {
    direction = fromStructure
    strength = 'weak'
  } else if ((fromStructure === null || fromStructure === 'range') && fromEma !== null) {
    direction = fromEma
    strength = fromEma === 'range' ? null : 'weak'
  } else if (fromStructure !== null && fromEma !== null) {
    // Struktura a EMA proti sobě → bez trendu (korekce uvnitř trendu)
    direction = 'range'
    strength = null
  }
  const notes: string[] = []
  if (structure) notes.push(`struktura ${structure.kind}`)
  else notes.push('struktura bez dvou swingů')
  if (emaReading) {
    notes.push(
      `${emaReading.priceAboveFast ? 'cena nad' : 'cena pod'} EMA20` +
        (emaReading.fastAboveSlow === null
          ? ''
          : `, EMA20 ${emaReading.fastAboveSlow ? 'nad' : 'pod'} EMA50`),
    )
  }
  if (strength === 'weak') notes.push('slabý — struktura a EMA se neshodnou')
  return { tf, candles: candles.length, direction, strength, structure, ema: emaReading, note: notes.join('; ') } // prettier-ignore
}

function majority(directions: (TrendDirection | null)[]): TrendDirection | null {
  const decided = directions.filter((value): value is TrendDirection => value !== null)
  if (decided.length === 0) return null
  const counts: Record<TrendDirection, number> = { up: 0, down: 0, range: 0 }
  for (const value of decided) counts[value] += 1
  if (counts.up > counts.down && counts.up >= counts.range) return 'up'
  if (counts.down > counts.up && counts.down >= counts.range) return 'down'
  return 'range'
}

/** Směr vyšších TF: shoda W a D, jinak rozhoduje D (W je kontext, D obchodní rámec). */
function higherDirection(byTf: Map<TimeframeKey, TimeframeTrend>): TrendDirection | null {
  const week = byTf.get('W')?.direction ?? null
  const day = byTf.get('D')?.direction ?? null
  if (day === null) return week
  return day
}

/** Sestaví report shora dolů; chybějící TF (bez dat z API) se přeskočí. */
export function assessTrends(candlesByTf: Partial<Record<TimeframeKey, Candle[]>>): TrendReport {
  const byTimeframe = TIMEFRAMES.map((tf) => assessTimeframe(tf, candlesByTf[tf] ?? []))
  const map = new Map(byTimeframe.map((row) => [row.tf, row] as const))
  const higher = higherDirection(map)
  const lower = majority(LOWER_TIMEFRAMES.map((tf) => map.get(tf)?.direction ?? null))
  const decidedRows = byTimeframe.filter((row) => row.direction !== null)
  const decided = decidedRows.length
  const aligned = higher === null ? 0 : decidedRows.filter((row) => row.direction === higher).length
  const week = map.get('W')?.direction ?? null
  const day = map.get('D')?.direction ?? null
  return {
    byTimeframe,
    higher,
    lower,
    aligned,
    decided,
    reading: buildReading({ higher, lower, week, day, aligned, decided }),
    expected: expectedDirection(higher, lower),
  }
}

/** Očekávaný směr z trendu: vyšší TF rozhoduje, souhlas nižších ho potvrzuje.
Vyšší bez trendu → bez převahy. Nižší proti vyššímu → převaha zůstává u vyššího,
ale slabší (verdikt dne to řeší vahami, #1090). */
export function expectedDirection(
  higher: TrendDirection | null,
  lower: TrendDirection | null,
): TrendDirection | null {
  if (higher === null) return lower === null || lower === 'range' ? null : lower
  if (higher === 'range') return null
  return higher
}

function buildReading(input: {
  higher: TrendDirection | null
  lower: TrendDirection | null
  week: TrendDirection | null
  day: TrendDirection | null
  aligned: number
  decided: number
}): string {
  const { higher, lower, week, day, aligned, decided } = input
  if (decided === 0) return 'Zatím málo svíček pro čtení trendu.'
  const parts: string[] = []
  if (week !== null && day !== null && week !== day && day === 'range') {
    parts.push(
      `Týden ${directionLabel(week)}, den bez trendu — konsolidace uvnitř týdenního trendu.`,
    )
  } else if (week !== null && day !== null && week !== day) {
    parts.push(
      `Týden ${directionLabel(week)}, den ${directionLabel(day)} — denní trend je korekce uvnitř týdenního.`,
    )
  } else if (higher !== null) {
    parts.push(`Vyšší TF: ${directionLabel(higher)}.`)
  }
  if (higher === null || higher === 'range') {
    parts.push('Vyšší TF bez trendu — den řídí úrovně a gamma režim, ne trend.')
    return parts.join(' ')
  }
  const side = higher === 'up' ? 'long' : 'short'
  const counter = higher === 'up' ? 'short' : 'long'
  if (lower === higher) {
    parts.push(
      `Nižší TF souhlasí (${aligned}/${decided}) — obchodovat ${side} ve směru trendu, ${counter} jen jako fade u zdí.`,
    )
  } else if (lower === null || lower === 'range') {
    parts.push(
      `Nižší TF bez trendu — čekat na obnovení směru vyššího TF (${side}), do té doby úrovně.`,
    )
  } else {
    parts.push(
      `Nižší TF korigují proti vyššímu (${aligned}/${decided} v souladu) — ${side} až po návratu nižších TF do směru, ${counter} jen s menší velikostí.`,
    )
  }
  return parts.join(' ')
}
