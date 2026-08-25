/** Načtení denního replay balíku (SPEC kap. 6 /replay) a příprava dat v paměti.

Balík se stahuje při načtení; přetáčení dne je pak čisté krájení polí v paměti
(AC issue #27: žádný fetch per frame). Snapshot matice chodí jako base64
Arrow IPC stream — dekóduje ji apache-arrow.

Živý provoz (#127): místo přenačítání celého balíku každou minutu se z WS kanálů
připojuje JEN nová minuta — `decodeBundle` → `ReplayInputs`, `appendMinute` přidá
minutu, `assembleReplayDay` z inputs poskládá `ReplayDay`. `buildReplayDay` je
složení obou (drží zpětně kompatibilní API i testy).
*/
import { tableFromIPC } from 'apache-arrow'
import { API_BASE } from '../config'
import type { PanelSeries } from '../components/BottomPanels'
import type { HeatmapGrid } from '../heatmap/grid'
import { buildModeGrid } from '../heatmap/modes'
import type { RawDay } from '../heatmap/modes'
import { maxPainSeries } from '../heatmap/maxpain'
import {
  LEVEL_COLORS,
  OI_WALL_DASH,
  SECONDARY_WALL_DASH,
  WALL_DOM_WEAK,
  lastLevelValue,
} from '../heatmap/overlays'
import type { LevelLine, OverlayData, PriceBar } from '../heatmap/overlays'
import type { ProfileRow } from '../profile/bars'

/** Profilové řádky per minuta počítané LÍNĚ (#142).

Zobrazuje se vždy jen jedna minuta, ale předpočítání celého dne alokovalo
`minuty × strikes` objektů při každém appendu — po `maxPain` a popiscích os to byl
největší zbylý náklad uzavření minuty (a držel desítky MB). Řádky se proto počítají
na vyžádání a cachují per minuta. */
export interface ProfileSource {
  readonly length: number
  /** Řádky dané minuty; mimo rozsah prázdné pole. */
  rowsAt(minuteIdx: number): ProfileRow[]
}

/** Obalí hotová data (Daily pohled, testy) do `ProfileSource`. */
export function profileSourceOf(rows: ProfileRow[][]): ProfileSource {
  return { length: rows.length, rowsAt: (minuteIdx) => rows[minuteIdx] ?? [] }
}

/** Dyn GEX profil minuty (ADR-0009, #203): NetGEX $/bod na cenové mřížce. */
export interface GexProfileRow {
  tsIso: string
  gridStart: number
  gridStep: number
  values: number[]
}

/** Modelované Dyn GEX pole (ADR-0009 fáze 2): budoucí sloupce s klesajícím τ.
Sloupec `k` odpovídá času colStart + k·colStepMin minut; drží se jen poslední stav. */
export interface GexFieldRow {
  tsIso: string
  gridStart: number
  gridStep: number
  colStartIso: string
  colStepMin: number
  colCount: number
  /** Sloupce za sebou: values[colIdx · gridLen + i], gridLen = values.length / colCount. */
  values: number[]
}

export interface ReplayDay {
  symbol: string
  expiry: string
  date: string
  minutes: string[] // ISO timestampy minut (osa X)
  grid: HeatmapGrid // celý den (výchozí OI mód, normalizace p99)
  /** Surová snapshot matice — přepínání módů/škál lokálně (SPEC 4.3). */
  raw: RawDay
  /** Surová matice s OI nahrazeným FA odhadem (#232, řada oiest); null =
  žádný odhad nedorazil. OI složky nesou OI_est, volume/vega zůstávají měřené. */
  rawFa: RawDay | null
  overlays: OverlayData // celý den
  panels: PanelSeries // celý den
  /** Profilové řádky per minuta (líné — krájení bez přepočtu celého dne). */
  profileByMinute: ProfileSource
  /** `minuteIdx` minut, jejichž bar je zatím provizorní (ADR-0005). Živá svíčka
  ze spotu je pro ně přesnější, takže jim v grafu ustupuje až finální bar. */
  provisionalMinutes: number[]
  /** Dyn GEX profil per minuta (ADR-0009); null = minuta profil nemá. */
  gexProfile: (GexProfileRow | null)[]
  /** Modelované pole budoucích sloupců (ADR-0009 fáze 2); null = bez pole. */
  gexField: GexFieldRow | null
  /** FA varianta Dyn GEX profilu (#232, řada gexprofilefa); null = bez FA řady. */
  gexProfileFa: (GexProfileRow | null)[] | null
  /** FA varianta modelovaného pole (#232, řada gexfieldfa). */
  gexFieldFa: GexFieldRow | null
  /** GEX žebřík per minuta (#244); null = minuta žebřík nemá. */
  ladder: (LadderMinuteRow | null)[]
}

/** GEX žebřík minuty (#244): významné striky per strana s podílem na síle. */
export interface LadderMinuteRow {
  tsIso: string
  callStrikes: number[]
  callShares: number[]
  putStrikes: number[]
  putShares: number[]
}

const LEVEL_KEYS = ['flip', 'centroid', 'call_wall', 'put_wall', 'call_wall_2', 'put_wall_2', 'call_wall_dom', 'put_wall_dom', 'call_wall_2_dom', 'put_wall_2_dom', 'fa_flip', 'fa_call_wall', 'fa_put_wall', 'oi_call_wall', 'oi_put_wall', 'oi_call_share', 'oi_put_share'] as const // prettier-ignore

interface BarInput {
  tsIso: string
  open?: number
  high?: number
  low?: number
  close: number
  volume: number
  /** `false` = provizorní bar rozdělané minuty (ADR-0005); chybí-li, bere se jako finální. */
  final?: boolean
}
interface LevelsInput {
  tsIso: string
  values: Record<string, number | null>
}
interface FlowInput {
  tsIso: string
  cum_delta: number
  /** CVD podkladu (#829); chybí/null = minuta bez dat (běh bez tasty větve). */
  futures_cvd?: number | null
}
interface OiPrevInput {
  strike: number
  right: string
  oi: number
}

/** Rozložený vstup dne — matice per-strike + řádky barů/levels/flow. Roste přes append. */
export interface ReplayInputs {
  symbol: string
  expiry: string
  date: string
  minutes: string[]
  /** Paralelně k `minutes`: měla minuta opční snapshot? (#459)
   *
   * Osa X je sjednocení minut ze snapshotů a barů, takže po výpadku sběru
   * existují sloupce, kde máme jen cenu. Matice tam nesou nuly a ty NEJSOU
   * měření — kdo z nich počítá přírůstky nebo profil, musí je přeskočit. */
  snapshotMinutes: boolean[]
  strikes: number[]
  callOi: Float32Array
  putOi: Float32Array
  /** FA odhad OI (#232): kopie měřených matic s buňkami přepsanými řadou
   * oiest. Drží se VŽDY (i bez odhadu — pak jsou identické), aby append
   * nemusel větvit tvar; `hasOiEst` říká, jestli nějaký odhad vůbec dorazil. */
  callOiEst: Float32Array
  putOiEst: Float32Array
  hasOiEst: boolean
  callVolume: Float32Array
  putVolume: Float32Array
  callDelta: Float32Array
  putDelta: Float32Array
  callVega: Float32Array
  putVega: Float32Array
  /** Midpoint (bid+ask)/2 per buňka (#469); 0 = kotace chybí. */
  callMid: Float32Array
  putMid: Float32Array
  staleAge: Float32Array
  bars: BarInput[]
  levels: LevelsInput[]
  flow: FlowInput[]
  oiPrev: OiPrevInput[]
  /** Denní OI dneška vč. striků mimo snapshoty (#849) — široký archiv z tasty
  (#828). Nese JEN OI: kotace, greeks ani volume pro tyhle striky neexistují. */
  oiToday: OiPrevInput[]
  gexProfile: GexProfileRow[]
  /** Modelované pole (ADR-0009 fáze 2) — jen poslední stav, starší se zahazuje. */
  gexField: GexFieldRow | null
  /** FA varianta Dyn GEX profilů/pole (#232) — prázdné = engine FA nepočítá. */
  gexProfileFa: GexProfileRow[]
  gexFieldFa: GexFieldRow | null
  /** GEX žebřík per minuta (#244). */
  ladder: LadderMinuteRow[]
  /** Klíče `minuta|strike|strana`, pro které OI není k dispozici (#465).
   *
   * Množina místo pole: profil se na ni ptá per řádek při každém překreslení,
   * takže lineární hledání by bylo O(strikes × chybějících) na minutu. */
  oiMissing: Set<string>
  /** Klíče `minuta|strike|strana` s OI doplněným z tasty Summary (#664) —
   * hodnota je měřená, jen z druhého feedu; množina drží původ čísla. */
  oiFilled: Set<string>
  /** Minuty, kde nadpoloviční část řetězu běží bez OI (#664): flip a spol.
   * tam stojí na řídké páteři (typicky 0DTE ráno do publikace CME) a kreslí
   * se ztlumeně. `null` = minuta bez snapshotů (nelze posoudit). */
  oiLowMinutes: (boolean | null)[]
  /** ISO minuty s příznakem catch_up (#518, ADR-0024): první sweep po startu
   * enginu uprostřed dne. Kumulativy v nich dohánějí celou dobu výpadku, takže
   * přírůstkové odvozeniny je čtou jako první měřenou minutu dne, ne jako
   * minutový obchod. */
  catchUpMinutes: Set<string>
}

/** Klíč do `ReplayInputs.oiMissing` — sjednocený tvar pro decode i append. */
export function oiMissingKey(tsIso: string, strike: number, right: string): string {
  return `${tsIso}|${strike}|${right}`
}

/** Práh podílu kontraktů bez OI, nad kterým je minuta „řídká" (#664).
 *
 * 12. 8. běžel 0DTE flip celý den na 4 kontraktech ze 160 a od reference se
 * lišil o ~50 b — nadpoloviční díra znamená, že flip není rovnocenná úroveň. */
export const OI_LOW_THRESHOLD = 0.5

/** Jedna živá minuta z WS kanálů (#127) — snapshot řez + volitelně bar/levels/flow. */
export interface LiveMinuteRow {
  strike: number
  right: 'C' | 'P'
  oi: number
  volume: number
  delta: number
  /** Vega pro VEX módy (#201) — starší engine pole neposílá. */
  vega?: number
  stale_age?: number
  /** Midpoint (bid+ask)/2 pro P/C v prémiích (#469); null/chybí = bez kotace. */
  mid?: number | null
}
export interface LiveMinute {
  tsIso: string
  rows: LiveMinuteRow[]
  /** Catch-up minuta (#518): první sweep po startu enginu uprostřed dne. */
  catchUp?: boolean
  bar?: {
    open?: number
    high?: number
    low?: number
    close: number
    volume?: number
    /** `false` = provizorní bar rozdělané minuty (ADR-0005). */
    final?: boolean
  }
  levels?: Record<string, number | null>
  flow?: { cum_delta: number; futures_cvd?: number | null }
  /** Dyn GEX profil minuty z WS kanálu gexprofile.* (ADR-0009). */
  gexProfile?: { grid_start: number; grid_step: number; values: number[] }
  /** OI odhady minuty z WS kanálu oiest.* (#232) — jen strany lišící se od měření. */
  oiEst?: Array<{ strike: number; right: 'C' | 'P'; oi_est: number }>
  /** FA Dyn GEX profil minuty z WS kanálu gexprofilefa.* (#232). */
  gexProfileFa?: { grid_start: number; grid_step: number; values: number[] }
  /** GEX žebřík minuty z WS kanálu ladder.* (#244). */
  ladder?: {
    call_strikes: number[]
    call_shares: number[]
    put_strikes: number[]
    put_shares: number[]
  }
  /** Modelované pole z WS kanálu gexfield.* (ADR-0009 fáze 2). */
  gexField?: {
    grid_start: number
    grid_step: number
    col_start: string
    col_step_min: number
    col_count: number
    values: number[]
  }
  /** FA modelované pole z WS kanálu gexfieldfa.* (#232). */
  gexFieldFa?: {
    grid_start: number
    grid_step: number
    col_start: string
    col_step_min: number
    col_count: number
    values: number[]
  }
}

interface ReplayBundle {
  symbol: string
  expiry: string
  date: string
  snapshots_arrow_base64: string
  levels: Array<Record<string, unknown>>
  /** Sekundární zdi (ADR-0008, #92) — starší API pole neposílá. */
  levels2?: Array<Record<string, unknown>>
  /** Dominance zdí (ADR-0010, #223) — starší API pole neposílá. */
  walldom?: Array<Record<string, unknown>>
  oiwalls?: Array<Record<string, unknown>>
  /** Flow-adjusted levels (ADR-0011, #222) — starší API pole neposílá. */
  levelsfa?: Array<Record<string, unknown>>
  /** GEX žebřík (#244) — starší API pole neposílá. */
  ladder?: Array<Record<string, unknown>>
  /** Striky bez OI (#465) — v běžný den prázdné, starší API klíč neposílá. */
  oimissing?: Array<Record<string, unknown>>
  /** Striky s OI z tasty Summary (#664) — bez fillu prázdné, starší API klíč neposílá. */
  oifilled?: Array<Record<string, unknown>>
  /** Catch-up minuty (#518, ADR-0024) — v běžný den prázdné, starší API klíč neposílá. */
  catchup?: Array<Record<string, unknown>>
  /** Dyn GEX profily (ADR-0009, #203) — starší API pole neposílá. */
  gexprofile?: Array<Record<string, unknown>>
  /** Modelované pole (ADR-0009 fáze 2) — starší API klíč neposílá. */
  gexfield?: Array<Record<string, unknown>>
  /** OI odhad z toku (#232) — jen strany lišící se od měřeného OI. */
  oiest?: Array<Record<string, unknown>>
  /** FA varianta Dyn GEX profilů/pole (#232) — starší API klíče neposílá. */
  gexprofilefa?: Array<Record<string, unknown>>
  gexfieldfa?: Array<Record<string, unknown>>
  flow: Array<Record<string, unknown>>
  bars: Array<Record<string, unknown>>
  /** OI téže expirace z předchozího archivovaného dne (ΔOI vs. včera). */
  oi_prev?: Array<{ strike: number; right: string; oi: number }>
  oi_today?: Array<{ strike: number; right: string; oi: number }>
}

export interface DayListing {
  date: string
  expiry: string
}

/** Seznam uložených dnů instrumentu (pro Daily pohled) — s expirací per den. */
export async function fetchDays(symbol: string): Promise<DayListing[]> {
  const response = await fetch(`${API_BASE}/instruments/${symbol}/days`)
  if (!response.ok) {
    throw new Error(`Seznam dnů ${symbol} selhal: HTTP ${response.status}`)
  }
  const payload = (await response.json()) as { days: DayListing[] }
  return payload.days
}

async function fetchBundle(symbol: string, expiry: string, date: string): Promise<ReplayBundle> {
  const response = await fetch(`${API_BASE}/replay/${symbol}/${expiry}/${date}`)
  if (!response.ok) {
    throw new Error(`Replay ${symbol}/${expiry}/${date} selhal: HTTP ${response.status}`)
  }
  return (await response.json()) as ReplayBundle
}

export async function fetchReplay(
  symbol: string,
  expiry: string,
  date: string,
): Promise<ReplayDay> {
  return assembleReplayDay(decodeBundle(await fetchBundle(symbol, expiry, date)))
}

/** Rozložený vstup dne z /replay (pro živý append). */
export async function fetchReplayInputs(
  symbol: string,
  expiry: string,
  date: string,
): Promise<ReplayInputs> {
  return decodeBundle(await fetchBundle(symbol, expiry, date))
}

/** Kanonický klíč minuty: Arrow vrací timestamp jako epoch (ms), JSON jako ISO string. */
export function canonicalTs(value: unknown): string {
  if (typeof value === 'number' || typeof value === 'bigint') {
    return new Date(Number(value)).toISOString()
  }
  if (value instanceof Date) return value.toISOString()
  const text = String(value)
  const asNumber = Number(text)
  if (Number.isFinite(asNumber) && !text.includes('-')) {
    return new Date(asNumber).toISOString()
  }
  const parsed = new Date(text)
  return Number.isNaN(parsed.getTime()) ? text : parsed.toISOString()
}

function base64ToBytes(base64: string): Uint8Array {
  const binary = atob(base64)
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index)
  }
  return bytes
}

function numOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** Řádky profilové řady z bundle → GexProfileRow[] (gexprofile i gexprofilefa). */
function decodeProfileRows(rows: Array<Record<string, unknown>> | undefined): GexProfileRow[] {
  return (rows ?? [])
    .map((row) => ({
      tsIso: canonicalTs(row.ts_min),
      gridStart: Number(row.grid_start),
      gridStep: Number(row.grid_step),
      values: Array.isArray(row.values) ? (row.values as number[]).map(Number) : [],
    }))
    .filter((row) => row.values.length > 0 && Number.isFinite(row.gridStart))
}

/** Poslední řádek řady pole → GexFieldRow (gexfield i gexfieldfa, #232). */
function decodeFieldRows(rows: Array<Record<string, unknown>> | undefined): GexFieldRow | null {
  const fieldRaw = (rows ?? []).at(-1)
  const fieldValues =
    fieldRaw && Array.isArray(fieldRaw.values) ? (fieldRaw.values as number[]).map(Number) : []
  const fieldColCount = fieldRaw ? Number(fieldRaw.col_count) : 0
  return fieldRaw && fieldColCount > 0 && fieldValues.length % fieldColCount === 0 && fieldValues.length > 0 // prettier-ignore
    ? {
        tsIso: canonicalTs(fieldRaw.ts_min),
        gridStart: Number(fieldRaw.grid_start),
        gridStep: Number(fieldRaw.grid_step),
        colStartIso: canonicalTs(fieldRaw.col_start),
        colStepMin: Number(fieldRaw.col_step_min),
        colCount: fieldColCount,
        values: fieldValues,
      }
    : null
}

/** Klíč aktuální wall-clock minuty (UTC) — detekce rozdělané minuty v bundle (#158). */
function currentMinuteIso(now: Date): string {
  const minute = new Date(now)
  minute.setUTCSeconds(0, 0)
  return minute.toISOString()
}

/** Dekóduje /replay balík na rozložený vstup (Arrow matice + řádky barů/levels/flow).

`now` je injektovatelný kvůli testům; default = skutečný čas. */
export function decodeBundle(bundle: ReplayBundle, now: Date = new Date()): ReplayInputs {
  const table = tableFromIPC(base64ToBytes(bundle.snapshots_arrow_base64))
  const tsColumn = table.getChild('ts_min')
  const strikeColumn = table.getChild('strike')
  const rightColumn = table.getChild('right')
  const volumeColumn = table.getChild('volume')
  const oiColumn = table.getChild('oi')
  const deltaColumn = table.getChild('delta')
  const staleColumn = table.getChild('stale_age')
  const vegaColumn = table.getChild('vega') // VEX módy (#201)
  const bidColumn = table.getChild('bid') // P/C v prémiích (#469)
  const askColumn = table.getChild('ask')
  if (!tsColumn || !strikeColumn || !rightColumn) {
    throw new Error('Replay balík: snapshot tabulka nemá očekávané sloupce')
  }

  // Osa X = sjednocení minut ze snapshotů a barů (#459). Backfill barů po
  // výpadku (#225) doplní cenu i pro minuty, kdy opční sweep neběžel; kdyby se
  // osa stavěla jen ze snapshotů, ty bary by se zahodily a graf by 46minutovou
  // díru vykreslil jako spojitý průběh. Řadí se podle času, ne podle pořadí
  // řádků — backfillovaná minuta patří doprostřed, ne na konec.
  const snapshotTs = new Set<string>()
  const strikeSet = new Set<number>()
  // Kontraktů (řádků) per minuta — jmenovatel podílu chybějícího OI (#664);
  // sjednocení striků přes den by ranní užší pásmo podhodnotilo
  const rowsByTs = new Map<string, number>()
  const rowCount = table.numRows
  for (let row = 0; row < rowCount; row += 1) {
    const ts = canonicalTs(tsColumn.get(row))
    snapshotTs.add(ts)
    rowsByTs.set(ts, (rowsByTs.get(ts) ?? 0) + 1)
    strikeSet.add(Number(strikeColumn.get(row)))
  }
  const barTs = bundle.bars.map((bar) => canonicalTs(bar.ts_min))
  const minuteKeys = [...new Set([...snapshotTs, ...barTs])].sort()
  const minuteIndex = new Map(minuteKeys.map((ts, index) => [ts, index]))
  const snapshotMinutes = minuteKeys.map((ts) => snapshotTs.has(ts))
  const strikes = [...strikeSet].sort((a, b) => a - b)
  const strikeIndex = new Map(strikes.map((strike, index) => [strike, index]))
  const minutes = minuteKeys.length
  const size = minutes * strikes.length

  const callOi = new Float32Array(size)
  const putOi = new Float32Array(size)
  const callVolume = new Float32Array(size)
  const putVolume = new Float32Array(size)
  const callDelta = new Float32Array(size)
  const putDelta = new Float32Array(size)
  const callVega = new Float32Array(size)
  const putVega = new Float32Array(size)
  const callMid = new Float32Array(size)
  const putMid = new Float32Array(size)
  const staleAge = new Float32Array(size)

  for (let row = 0; row < rowCount; row += 1) {
    const minuteIdx = minuteIndex.get(canonicalTs(tsColumn.get(row)))!
    const strikeIdx = strikeIndex.get(Number(strikeColumn.get(row)))!
    const index = strikeIdx * minutes + minuteIdx
    const right = String(rightColumn.get(row)) as 'C' | 'P'
    const oi = Number(oiColumn?.get(row) ?? 0) || 0
    const volume = Number(volumeColumn?.get(row) ?? 0) || 0
    const delta = Number(deltaColumn?.get(row) ?? 0) || 0
    const vega = Number(vegaColumn?.get(row) ?? 0) || 0
    const bid = Number(bidColumn?.get(row) ?? 0) || 0
    const ask = Number(askColumn?.get(row) ?? 0) || 0
    const mid = bid > 0 && ask > 0 ? (bid + ask) / 2 : 0
    if (right === 'C') {
      callOi[index] = oi
      callVolume[index] = volume
      callDelta[index] = delta
      callVega[index] = vega
      callMid[index] = mid
    } else {
      putOi[index] = oi
      putVolume[index] = volume
      putDelta[index] = delta
      putVega[index] = vega
      putMid[index] = mid
    }
    staleAge[index] = Math.max(staleAge[index], Number(staleColumn?.get(row) ?? 0) || 0)
  }

  // Parquet finalitu nenese (ADR-0005), ale bar AKTUÁLNÍ wall-clock minuty je
  // s jistotou provizorní (engine ho upsertuje v :54 rozdělané minuty). Bez
  // označení by po refreshi uprostřed minuty svíčka zmrzla až do dalšího cyklu,
  // přestože spot ticky tečou hned — provizorní minutě spot svíčka přebírá (#158).
  const liveMinuteIso = currentMinuteIso(now)
  const bars: BarInput[] = bundle.bars.map((bar) => {
    const open = Number(bar.open)
    const high = Number(bar.high)
    const low = Number(bar.low)
    const tsIso = canonicalTs(bar.ts_min)
    return {
      tsIso,
      close: Number(bar.close),
      volume: Number(bar.volume) || 0,
      open: Number.isFinite(open) ? open : undefined,
      high: Number.isFinite(high) ? high : undefined,
      low: Number.isFinite(low) ? low : undefined,
      final: tsIso !== liveMinuteIso,
    }
  })
  // Levels + sekundární zdi (vlastní řada levels2, ADR-0008) sloučené per minuta
  const levelsByTs = new Map<string, LevelsInput>()
  for (const row of bundle.levels) {
    const tsIso = canonicalTs(row.ts_min)
    levelsByTs.set(tsIso, {
      tsIso,
      values: Object.fromEntries(LEVEL_KEYS.map((key) => [key, numOrNull(row[key])])),
    })
  }
  for (const row of bundle.levels2 ?? []) {
    const tsIso = canonicalTs(row.ts_min)
    const entry = levelsByTs.get(tsIso) ?? {
      tsIso,
      values: Object.fromEntries(LEVEL_KEYS.map((key) => [key, null])),
    }
    entry.values.call_wall_2 = numOrNull(row.call_wall_2)
    entry.values.put_wall_2 = numOrNull(row.put_wall_2)
    levelsByTs.set(tsIso, entry)
  }
  // OI zdi (#851) — vlastní řada, merge per minuta jako levels2. Jiná
  // veličina než gamma zdi: maximum otevřeného zájmu, ne maximum NetGEX.
  for (const row of bundle.oiwalls ?? []) {
    const tsIso = canonicalTs(row.ts_min)
    const entry = levelsByTs.get(tsIso) ?? {
      tsIso,
      values: Object.fromEntries(LEVEL_KEYS.map((key) => [key, null])),
    }
    entry.values.oi_call_wall = numOrNull(row.oi_call_wall)
    entry.values.oi_put_wall = numOrNull(row.oi_put_wall)
    entry.values.oi_call_share = numOrNull(row.oi_call_share)
    entry.values.oi_put_share = numOrNull(row.oi_put_share)
    levelsByTs.set(tsIso, entry)
  }

  // Dominance zdí (ADR-0010, #223) — vlastní řada, merge per minuta jako levels2
  for (const row of bundle.walldom ?? []) {
    const tsIso = canonicalTs(row.ts_min)
    const entry = levelsByTs.get(tsIso) ?? {
      tsIso,
      values: Object.fromEntries(LEVEL_KEYS.map((key) => [key, null])),
    }
    entry.values.call_wall_dom = numOrNull(row.call_wall_dom)
    entry.values.put_wall_dom = numOrNull(row.put_wall_dom)
    entry.values.call_wall_2_dom = numOrNull(row.call_wall_2_dom)
    entry.values.put_wall_2_dom = numOrNull(row.put_wall_2_dom)
    levelsByTs.set(tsIso, entry)
  }
  // Flow-adjusted levels (ADR-0011, #222) — vlastní řada, klíče s prefixem fa_
  for (const row of bundle.levelsfa ?? []) {
    const tsIso = canonicalTs(row.ts_min)
    const entry = levelsByTs.get(tsIso) ?? {
      tsIso,
      values: Object.fromEntries(LEVEL_KEYS.map((key) => [key, null])),
    }
    entry.values.fa_flip = numOrNull(row.flip)
    entry.values.fa_call_wall = numOrNull(row.call_wall)
    entry.values.fa_put_wall = numOrNull(row.put_wall)
    levelsByTs.set(tsIso, entry)
  }
  const levels = [...levelsByTs.values()]
  const flow: FlowInput[] = bundle.flow.map((row) => ({
    tsIso: canonicalTs(row.ts_min),
    cum_delta: Number(row.cum_delta) || 0,
    futures_cvd: numOrNull(row.futures_cvd),
  }))

  // Dyn GEX profily (ADR-0009) — starší API klíč neposílá; FA varianta (#232)
  const gexProfile = decodeProfileRows(bundle.gexprofile)
  const gexProfileFa = decodeProfileRows(bundle.gexprofilefa)

  // GEX žebřík (#244) — starší API klíč neposílá
  const ladderRows: LadderMinuteRow[] = (bundle.ladder ?? []).map((row) => ({
    tsIso: canonicalTs(row.ts_min),
    callStrikes: Array.isArray(row.call_strikes) ? (row.call_strikes as number[]).map(Number) : [],
    callShares: Array.isArray(row.call_shares) ? (row.call_shares as number[]).map(Number) : [],
    putStrikes: Array.isArray(row.put_strikes) ? (row.put_strikes as number[]).map(Number) : [],
    putShares: Array.isArray(row.put_shares) ? (row.put_shares as number[]).map(Number) : [],
  }))

  // Striky bez OI (#465) — v běžný den řada neexistuje a množina zůstane prázdná
  const oiMissing = new Set<string>(
    (bundle.oimissing ?? []).map((row) =>
      oiMissingKey(canonicalTs(row.ts_min), Number(row.strike), String(row.right)),
    ),
  )
  // Striky s OI z tasty Summary (#664) — v oiMissing nejsou (hodnota je měřená)
  const oiFilled = new Set<string>(
    (bundle.oifilled ?? []).map((row) =>
      oiMissingKey(canonicalTs(row.ts_min), Number(row.strike), String(row.right)),
    ),
  )
  // Minuty s řídkou OI páteří (#664): podíl kontraktů bez OI nad prahem.
  // Tasty fill díry zmenšuje sám od sebe — doplněné řádky v oimissing nejsou.
  const missingByTs = new Map<string, number>()
  for (const row of bundle.oimissing ?? []) {
    const ts = canonicalTs(row.ts_min)
    missingByTs.set(ts, (missingByTs.get(ts) ?? 0) + 1)
  }
  const oiLowMinutes: (boolean | null)[] = minuteKeys.map((ts) => {
    const total = rowsByTs.get(ts) ?? 0
    if (total === 0) return null
    return (missingByTs.get(ts) ?? 0) / total > OI_LOW_THRESHOLD
  })

  // Catch-up minuty (#518) — v běžný den řada neexistuje a množina zůstane prázdná
  const catchUpMinutes = new Set<string>(
    (bundle.catchup ?? []).map((row) => canonicalTs(row.ts_min)),
  )

  // Modelované pole (ADR-0009 fáze 2) — partice drží jen poslední stav
  const gexField = decodeFieldRows(bundle.gexfield)
  const gexFieldFa = decodeFieldRows(bundle.gexfieldfa)

  // FA odhad OI (#232): kopie měřených matic s buňkami přepsanými řadou oiest.
  // Měřené matice zůstávají NEDOTČENÉ — měřený režim musí být bit-identický
  // s chováním bez FA vrstvy.
  const callOiEst = callOi.slice()
  const putOiEst = putOi.slice()
  const oiestRows = bundle.oiest ?? []
  for (const row of oiestRows) {
    const minuteIdx = minuteIndex.get(canonicalTs(row.ts_min))
    const strikeIdx = strikeIndex.get(Number(row.strike))
    const est = Number(row.oi_est)
    if (minuteIdx === undefined || strikeIdx === undefined || !Number.isFinite(est)) continue
    const index = strikeIdx * minutes + minuteIdx
    if (String(row.right) === 'C') callOiEst[index] = est
    else putOiEst[index] = est
  }

  return {
    symbol: bundle.symbol,
    expiry: bundle.expiry,
    date: bundle.date,
    minutes: minuteKeys,
    snapshotMinutes,
    strikes,
    callOi,
    putOi,
    callOiEst,
    putOiEst,
    hasOiEst: oiestRows.length > 0,
    callVolume,
    putVolume,
    callDelta,
    putDelta,
    callVega,
    putVega,
    callMid,
    putMid,
    staleAge,
    bars,
    levels,
    flow,
    oiToday: (bundle.oi_today ?? []).map((row) => ({
      strike: Number(row.strike),
      right: String(row.right),
      oi: Number(row.oi) || 0,
    })),
    oiPrev: (bundle.oi_prev ?? []).map((row) => ({
      strike: Number(row.strike),
      right: String(row.right),
      oi: Number(row.oi) || 0,
    })),
    gexProfile,
    gexField,
    gexProfileFa,
    gexFieldFa,
    ladder: ladderRows,
    oiMissing,
    oiFilled,
    oiLowMinutes,
    catchUpMinutes,
  }
}

function upsertRow<T extends { tsIso: string }>(rows: T[], row: T): T[] {
  const idx = rows.findIndex((existing) => existing.tsIso === row.tsIso)
  if (idx === -1) return [...rows, row]
  const copy = rows.slice()
  copy[idx] = row
  return copy
}

/** Připojí (nebo přepíše poslední) minutu do rozloženého vstupu — realokuje matice. */
export function appendMinute(inputs: ReplayInputs, minute: LiveMinute): ReplayInputs {
  const tsIso = canonicalTs(minute.tsIso)
  const existingIdx = inputs.minutes.indexOf(tsIso)
  const isAppend = existingIdx === -1
  const oldMinutes = inputs.minutes.length
  // Nová minuta patří tam, kam ji řadí čas — po #459 může osa nést bar-only
  // minuty, takže „vždycky na konec" už neplatí. Běžný živý případ (novější než
  // vše ostatní) vyjde na konec i tak, jen se k němu dojde přes hledání.
  const insertAt = isAppend ? inputs.minutes.findIndex((existing) => existing > tsIso) : existingIdx
  const targetMinute = isAppend && insertAt === -1 ? oldMinutes : insertAt
  const newMinutes = isAppend
    ? [...inputs.minutes.slice(0, targetMinute), tsIso, ...inputs.minutes.slice(targetMinute)]
    : inputs.minutes
  const newMinuteCount = newMinutes.length
  // Vsunutí doprostřed posouvá všechny minuty za sebou o jedna
  const shift = (minuteIdx: number): number =>
    isAppend && minuteIdx >= targetMinute ? minuteIdx + 1 : minuteIdx
  const snapshotMinutes = isAppend
    ? [
        ...inputs.snapshotMinutes.slice(0, targetMinute),
        minute.rows.length > 0,
        ...inputs.snapshotMinutes.slice(targetMinute),
      ]
    : inputs.snapshotMinutes.map((has, index) =>
        index === targetMinute ? has || minute.rows.length > 0 : has,
      )
  // Živá minuta oimissing řadu nenese (WS ji neposílá) — stejně jako plný
  // build bez záznamů: minuta se snapshoty = false, bar-only = null. Případnou
  // ranní díru doplní zpětně refetch bundle; živou poctivost nese OI badge
  // ze status kanálu (#664)
  const oiLowMinutes = isAppend
    ? [
        ...inputs.oiLowMinutes.slice(0, targetMinute),
        minute.rows.length > 0 ? false : null,
        ...inputs.oiLowMinutes.slice(targetMinute),
      ]
    : inputs.oiLowMinutes.map((low, index) =>
        index === targetMinute && low === null && minute.rows.length > 0 ? false : low,
      )

  const strikeSet = new Set(inputs.strikes)
  for (const row of minute.rows) strikeSet.add(row.strike)
  const strikesChanged = strikeSet.size !== inputs.strikes.length
  const newStrikes = strikesChanged ? [...strikeSet].sort((a, b) => a - b) : inputs.strikes
  const strikeCount = newStrikes.length
  const newStrikeIndex = new Map(newStrikes.map((strike, index) => [strike, index]))

  const size = strikeCount * newMinuteCount
  const callOi = new Float32Array(size)
  const putOi = new Float32Array(size)
  const callOiEst = new Float32Array(size)
  const putOiEst = new Float32Array(size)
  const callVolume = new Float32Array(size)
  const putVolume = new Float32Array(size)
  const callDelta = new Float32Array(size)
  const putDelta = new Float32Array(size)
  const callVega = new Float32Array(size)
  const putVega = new Float32Array(size)
  const callMid = new Float32Array(size)
  const putMid = new Float32Array(size)
  const staleAge = new Float32Array(size)

  // Přenos starých buněk na nový stride (minutes je násobitel řádku, viz grid.ts)
  for (let oldStrikeIdx = 0; oldStrikeIdx < inputs.strikes.length; oldStrikeIdx += 1) {
    const newStrikeIdx = newStrikeIndex.get(inputs.strikes[oldStrikeIdx])!
    for (let minuteIdx = 0; minuteIdx < oldMinutes; minuteIdx += 1) {
      const from = oldStrikeIdx * oldMinutes + minuteIdx
      const to = newStrikeIdx * newMinuteCount + shift(minuteIdx)
      callOi[to] = inputs.callOi[from]
      putOi[to] = inputs.putOi[from]
      callOiEst[to] = inputs.callOiEst[from]
      putOiEst[to] = inputs.putOiEst[from]
      callVolume[to] = inputs.callVolume[from]
      putVolume[to] = inputs.putVolume[from]
      callDelta[to] = inputs.callDelta[from]
      putDelta[to] = inputs.putDelta[from]
      callVega[to] = inputs.callVega[from]
      putVega[to] = inputs.putVega[from]
      callMid[to] = inputs.callMid[from]
      putMid[to] = inputs.putMid[from]
      staleAge[to] = inputs.staleAge[from]
    }
  }
  // Nová minuta ze snapshot řezu; odhad = měření, dokud ho oiest nepřepíše
  for (const row of minute.rows) {
    const to = newStrikeIndex.get(row.strike)! * newMinuteCount + targetMinute
    if (row.right === 'C') {
      callOi[to] = row.oi
      callOiEst[to] = row.oi
      callVolume[to] = row.volume
      callDelta[to] = row.delta
      callVega[to] = row.vega ?? 0
      callMid[to] = row.mid ?? 0
    } else {
      putOi[to] = row.oi
      putOiEst[to] = row.oi
      putVolume[to] = row.volume
      putDelta[to] = row.delta
      putVega[to] = row.vega ?? 0
      putMid[to] = row.mid ?? 0
    }
    staleAge[to] = Math.max(staleAge[to], row.stale_age ?? 0)
  }
  // OI odhady minuty z kanálu oiest.* (#232) — jen strany lišící se od měření
  for (const row of minute.oiEst ?? []) {
    const strikeIdx = newStrikeIndex.get(row.strike)
    if (strikeIdx === undefined || !Number.isFinite(row.oi_est)) continue
    const to = strikeIdx * newMinuteCount + targetMinute
    if (row.right === 'C') callOiEst[to] = row.oi_est
    else putOiEst[to] = row.oi_est
  }

  const bars = minute.bar
    ? upsertRow(inputs.bars, { tsIso, ...minute.bar, volume: minute.bar.volume ?? 0 })
    : inputs.bars
  const levels = minute.levels
    ? upsertRow(inputs.levels, {
        tsIso,
        values: Object.fromEntries(LEVEL_KEYS.map((key) => [key, minute.levels?.[key] ?? null])),
      })
    : inputs.levels
  const flow = minute.flow
    ? upsertRow(inputs.flow, { tsIso, cum_delta: minute.flow.cum_delta })
    : inputs.flow
  const gexProfile = minute.gexProfile
    ? upsertRow(inputs.gexProfile, {
        tsIso,
        gridStart: minute.gexProfile.grid_start,
        gridStep: minute.gexProfile.grid_step,
        values: minute.gexProfile.values,
      })
    : inputs.gexProfile
  const gexProfileFa = minute.gexProfileFa
    ? upsertRow(inputs.gexProfileFa, {
        tsIso,
        gridStart: minute.gexProfileFa.grid_start,
        gridStep: minute.gexProfileFa.grid_step,
        values: minute.gexProfileFa.values,
      })
    : inputs.gexProfileFa
  const ladder = minute.ladder
    ? upsertRow(inputs.ladder, {
        tsIso,
        callStrikes: minute.ladder.call_strikes,
        callShares: minute.ladder.call_shares,
        putStrikes: minute.ladder.put_strikes,
        putShares: minute.ladder.put_shares,
      })
    : inputs.ladder
  // Modelované pole: nová minuta prostě nahradí staré (jen poslední stav)
  const gexField = minute.gexField
    ? {
        tsIso,
        gridStart: minute.gexField.grid_start,
        gridStep: minute.gexField.grid_step,
        colStartIso: minute.gexField.col_start,
        colStepMin: minute.gexField.col_step_min,
        colCount: minute.gexField.col_count,
        values: minute.gexField.values,
      }
    : inputs.gexField
  const gexFieldFa = minute.gexFieldFa
    ? {
        tsIso,
        gridStart: minute.gexFieldFa.grid_start,
        gridStep: minute.gexFieldFa.grid_step,
        colStartIso: minute.gexFieldFa.col_start,
        colStepMin: minute.gexFieldFa.col_step_min,
        colCount: minute.gexFieldFa.col_count,
        values: minute.gexFieldFa.values,
      }
    : inputs.gexFieldFa

  // Catch-up minuta z WS (#518): první živý sweep po restartu enginu se musí
  // označit i bez refetche balíku — jinak by Opt Vol/Δ Flow vykreslily skok
  // kumulativu jako obří bar až do hodinové rekonciliace
  const catchUpMinutes = minute.catchUp
    ? new Set(inputs.catchUpMinutes).add(tsIso)
    : inputs.catchUpMinutes

  // Chybějící OI chodí zatím jen v /replay balíku, ne po WS — živě příchozí
  // minuta množinu nemění, ale musí ji propustit dál (jinak by ji append
  // zahodil a šrafování by po první živé minutě zmizelo)
  return {
    ...inputs,
    catchUpMinutes,
    minutes: newMinutes,
    snapshotMinutes,
    oiLowMinutes,
    strikes: newStrikes,
    callOi,
    putOi,
    callOiEst,
    putOiEst,
    hasOiEst: inputs.hasOiEst || (minute.oiEst?.length ?? 0) > 0,
    callVolume,
    putVolume,
    callDelta,
    putDelta,
    callVega,
    putVega,
    callMid,
    putMid,
    staleAge,
    bars,
    levels,
    flow,
    gexProfile,
    gexField,
    gexProfileFa,
    gexFieldFa,
    ladder,
  }
}

/** Poskládá `ReplayDay` (grid/overlays/panels/profil) z rozloženého vstupu. */
export function assembleReplayDay(inputs: ReplayInputs): ReplayDay {
  const { strikes } = inputs
  const minuteKeys = inputs.minutes
  const minutes = minuteKeys.length
  const minuteIndex = new Map(minuteKeys.map((ts, index) => [ts, index]))

  // Overlaye: cena z barů
  const price: PriceBar[] = []
  const provisionalMinutes: number[] = []
  let previousClose = Number.NaN
  // Bary se řadí podle osy, ne podle pořadí příchodu (#459): dozadu doplněná
  // minuta (backfill, opožděný bar) by jinak skončila na konci pole a `up` by
  // se počítalo vůči špatnému sousedovi — svíčka by dostala obrácenou barvu
  const orderedBars = [...inputs.bars].sort(
    (a, b) => (minuteIndex.get(a.tsIso) ?? -1) - (minuteIndex.get(b.tsIso) ?? -1),
  )
  for (const bar of orderedBars) {
    const minuteIdx = minuteIndex.get(bar.tsIso)
    if (minuteIdx !== undefined && Number.isFinite(bar.close)) {
      price.push({
        minuteIdx,
        close: bar.close,
        up: !(bar.close < previousClose),
        open: bar.open,
        high: bar.high,
        low: bar.low,
      })
      if (bar.final === false) provisionalMinutes.push(minuteIdx)
      previousClose = bar.close
    }
  }
  const levelSeries = (key: string): (number | null)[] => {
    const series: (number | null)[] = Array.from({ length: minutes }, () => null)
    for (const row of inputs.levels) {
      const minuteIdx = minuteIndex.get(row.tsIso)
      if (minuteIdx !== undefined) series[minuteIdx] = row.values[key] ?? null
    }
    return series
  }

  const spotSeries: (number | null)[] = Array.from({ length: minutes }, () => null)
  for (const bar of price) spotSeries[bar.minuteIdx] = bar.close
  const raw: RawDay = {
    minutes,
    strikes,
    callOi: inputs.callOi,
    putOi: inputs.putOi,
    callVolume: inputs.callVolume,
    putVolume: inputs.putVolume,
    callVega: inputs.callVega,
    putVega: inputs.putVega,
    spotSeries,
    staleAge: inputs.staleAge,
  }
  const grid = buildModeGrid(raw, 'oi', 'linear')
  // FA zdroj (#232): tatáž matice s OI_est místo měřeného OI — módy heatmapy
  // se nad ní přepínají stejně. Bez odhadu je null a UI padá na měřené.
  const rawFa: RawDay | null = inputs.hasOiEst
    ? { ...raw, callOi: inputs.callOiEst, putOi: inputs.putOiEst }
    : null

  // Minuta bez snapshotu nese v maticích nuly, ne měření (#459) — Max Pain z
  // nulového OI by ukázal libovolný strike, takže se v díře nekreslí vůbec
  const hasSnapshot = (minuteIdx: number): boolean => inputs.snapshotMinutes[minuteIdx] !== false
  const maxPain = maxPainSeries(raw).map((value, minuteIdx) =>
    hasSnapshot(minuteIdx) ? value : null,
  )

  // Flip na řídké OI páteři (#664): minuty s nadpoloviční dírou v OI se kreslí
  // ztlumeně a je-li řídká i poslední měřená minuta, cenovka nese varování —
  // flip z pár kontraktů není rovnocenná úroveň (12. 8.: 0DTE ~50 b vedle)
  const lastLowOi = ((): boolean => {
    for (let idx = inputs.oiLowMinutes.length - 1; idx >= 0; idx -= 1) {
      const low = inputs.oiLowMinutes[idx]
      if (low !== null) return low
    }
    return false
  })()
  const levels: LevelLine[] = [
    { name: 'flip', color: LEVEL_COLORS.flip, series: levelSeries('flip'), weak: inputs.oiLowMinutes, labelSuffix: lastLowOi ? ' ⚠ řídké OI' : undefined }, // prettier-ignore
    { name: 'centroid', color: LEVEL_COLORS.centroid, series: levelSeries('centroid') },
    { name: 'max_pain', color: LEVEL_COLORS.max_pain, series: maxPain },
    // Flow-adjusted levels (ADR-0011, #222): ODHAD z ranního OI + klasifikovaného
    // toku — čárkovaně (vizuální signál „model, ne měření"), přepínač „FA levels"
    { name: 'fa_flip', color: 'rgba(232,193,75,0.75)', dash: [8, 4], series: levelSeries('fa_flip') }, // prettier-ignore
    { name: 'fa_call_wall', color: 'rgba(62,207,142,0.75)', dash: [8, 4], series: levelSeries('fa_call_wall') }, // prettier-ignore
    { name: 'fa_put_wall', color: 'rgba(240,97,109,0.75)', dash: [8, 4], series: levelSeries('fa_put_wall') }, // prettier-ignore
  ]
  // Dominance zdí (ADR-0010, #223): slabá zeď (pod prahem) se kreslí ztlumeně,
  // cenovka primární zdi nese aktuální dominanci v %
  const weakFlags = (domKey: string): (boolean | null)[] =>
    levelSeries(domKey).map((dom) => (dom === null ? null : dom < WALL_DOM_WEAK))
  const domSuffix = (domKey: string): string | undefined => {
    const dom = lastLevelValue(levelSeries(domKey))
    return dom === null ? undefined : ` · ${Math.round(dom * 100)} %`
  }
  // Podíl OI zdi na své straně (#851) — nízké číslo = plochý profil, „zeď"
  // je jen nejvyšší z mnoha srovnatelných striků
  const shareSuffix = (key: string): string | undefined => {
    const share = lastLevelValue(levelSeries(key))
    return share === null ? undefined : ` · OI ${Math.round(share * 100)} %`
  }
  const walls: LevelLine[] = [
    { name: 'call_wall', color: LEVEL_COLORS.call_wall, series: levelSeries('call_wall'), weak: weakFlags('call_wall_dom'), labelSuffix: domSuffix('call_wall_dom') }, // prettier-ignore
    { name: 'put_wall', color: LEVEL_COLORS.put_wall, series: levelSeries('put_wall'), weak: weakFlags('put_wall_dom'), labelSuffix: domSuffix('put_wall_dom') }, // prettier-ignore
    // Sekundární zdi (ADR-0008): App je podle přepínače spáruje s primární
    // po úrovních, nebo zahodí; kreslí se tečkovaně a bez cenovky
    { name: 'call_wall_2', color: LEVEL_COLORS.call_wall_2, dash: SECONDARY_WALL_DASH, series: levelSeries('call_wall_2'), weak: weakFlags('call_wall_2_dom') }, // prettier-ignore
    { name: 'put_wall_2', color: LEVEL_COLORS.put_wall_2, dash: SECONDARY_WALL_DASH, series: levelSeries('put_wall_2'), weak: weakFlags('put_wall_2_dom') }, // prettier-ignore
    // OI zdi (#851): NEJSOU dopočítané z gammy — jsou to maxima otevřeného
    // zájmu, tedy jiná veličina (magnet k expiraci vs. hedging teď). Kreslí
    // se tečkovaně a nesou podíl na OI své strany, aby šlo poznat, jestli je
    // to koncentrovaná úroveň, nebo jen nejvyšší z mnoha srovnatelných.
    { name: 'oi_call_wall', color: LEVEL_COLORS.oi_call_wall, dash: OI_WALL_DASH, series: levelSeries('oi_call_wall'), labelSuffix: shareSuffix('oi_call_share') }, // prettier-ignore
    { name: 'oi_put_wall', color: LEVEL_COLORS.oi_put_wall, dash: OI_WALL_DASH, series: levelSeries('oi_put_wall'), labelSuffix: shareSuffix('oi_put_share') }, // prettier-ignore
  ]
  const overlays: OverlayData = {
    price,
    levels,
    walls,
    sessions: [],
    timestamp: minuteKeys.at(-1) ?? inputs.date,
  }

  const vol = Array.from({ length: minutes }, () => 0)
  for (const bar of inputs.bars) {
    const minuteIdx = minuteIndex.get(bar.tsIso)
    if (minuteIdx !== undefined) vol[minuteIdx] = bar.volume
  }
  // Přírůstkové panely počítají rozdíl vůči PŘEDCHOZÍ MĚŘENÉ minutě, ne vůči
  // index-1 (#459): sloupec bez snapshotu má nulové kumulativní volume, takže
  // by za dírou vyskočil falešný špic o velikosti celého dne.
  // Catch-up minuta (#518) se chová jako první měřená minuta dne — její
  // kumulativy dohánějí celou dobu výpadku, ne jednu minutu obchodů.
  const isCatchUp = (minuteIdx: number): boolean => inputs.catchUpMinutes.has(minuteKeys[minuteIdx])
  const measured = measuredMinutes(inputs.snapshotMinutes, minutes, isCatchUp)
  const optVolCall = optVolSeries(inputs.callVolume, minutes, strikes.length, measured)
  const optVolPut = optVolSeries(inputs.putVolume, minutes, strikes.length, measured)
  // Evo OI (#573): Σ OI přes striky per minuta; minuty bez snapshotu drží
  // předchozí hodnotu (schod) — nula by lhala „pozice zmizely"
  const evoOiCall = oiTotalSeries(inputs.callOi, minutes, strikes.length, hasSnapshot)
  const evoOiPut = oiTotalSeries(inputs.putOi, minutes, strikes.length, hasSnapshot)
  const deltaFlowCall = deltaFlowSeries(
    inputs.callVolume,
    inputs.callDelta,
    minutes,
    strikes.length,
    measured,
  )
  const deltaFlowPut = deltaFlowSeries(
    inputs.putVolume,
    inputs.putDelta,
    minutes,
    strikes.length,
    measured,
  )
  // CumΔ je kumulativní: v díře držíme poslední známou hodnotu. Nula by tvrdila,
  // že se tok vynuloval a zase vrátil — plochý úsek říká „nevíme o přírůstku",
  // což je stav, ve kterém jsme opravdu byli.
  const cumDelta = Array.from({ length: minutes }, () => 0)
  const cumDeltaByMinute = new Map<number, number>()
  // CVD podkladu (#829) drží stejnou logiku děr, ale null zůstává null:
  // linka se přeruší místo aby předstírala vyrovnaný tok
  const futuresCvd: (number | null)[] = Array.from({ length: minutes }, () => null)
  const cvdByMinute = new Map<number, number>()
  for (const row of inputs.flow) {
    const minuteIdx = minuteIndex.get(row.tsIso)
    if (minuteIdx !== undefined) {
      cumDeltaByMinute.set(minuteIdx, row.cum_delta)
      if (row.futures_cvd != null) cvdByMinute.set(minuteIdx, row.futures_cvd)
    }
  }
  let lastCumDelta = 0
  let lastCvd: number | null = null
  for (let minuteIdx = 0; minuteIdx < minutes; minuteIdx += 1) {
    lastCumDelta = cumDeltaByMinute.get(minuteIdx) ?? lastCumDelta
    cumDelta[minuteIdx] = lastCumDelta
    const cvd = cvdByMinute.get(minuteIdx)
    if (cvd !== undefined) lastCvd = cvd
    futuresCvd[minuteIdx] = lastCvd
  }

  // ΔOI vs. předchozí archivovaný den téže expirace (null = není srovnání)
  const prevOi = new Map<string, number>()
  for (const row of inputs.oiPrev) prevOi.set(`${row.strike}|${row.right}`, row.oi)
  const totalOiToday =
    inputs.callOi.reduce((sum, value) => sum + value, 0) +
    inputs.putOi.reduce((sum, value) => sum + value, 0)
  const oiChangeReady = prevOi.size > 0 && totalOiToday > 0

  // Řádky profilu se počítají až při dotazu na konkrétní minutu a cachují se (#142)
  const profileCache = new Map<number, ProfileRow[]>()
  const profileByMinute: ProfileSource = {
    length: minutes,
    rowsAt(minuteIdx: number): ProfileRow[] {
      if (minuteIdx < 0 || minuteIdx >= minutes) return []
      // Minuta bez snapshotu (#459): profil z nul by ukázal prázdné pruhy jako
      // měření, že v celém řetězu není žádné OI
      if (!hasSnapshot(minuteIdx)) return []
      const cached = profileCache.get(minuteIdx)
      if (cached) return cached
      const spotAtMinute = price.find((bar) => bar.minuteIdx === minuteIdx)?.close ?? Number.NaN
      const rows: ProfileRow[] = strikes.map((strike, strikeIdx) => {
        const index = strikeIdx * minutes + minuteIdx
        const callAbsDelta = Math.abs(inputs.callDelta[index])
        const putAbsDelta = Math.abs(inputs.putDelta[index])
        return {
          strike,
          callVolComponent: inputs.callVolume[index] * callAbsDelta,
          callOiComponent: inputs.callOi[index] * callAbsDelta,
          putVolComponent: inputs.putVolume[index] * putAbsDelta,
          putOiComponent: inputs.putOi[index] * putAbsDelta,
          callVolume: inputs.callVolume[index],
          putVolume: inputs.putVolume[index],
          callOi: inputs.callOi[index],
          putOi: inputs.putOi[index],
          distanceFromSpot: Number.isFinite(spotAtMinute) ? strike - spotAtMinute : 0,
          callOiChange: oiChangeReady
            ? inputs.callOi[index] - (prevOi.get(`${strike}|C`) ?? 0)
            : null,
          putOiChange: oiChangeReady
            ? inputs.putOi[index] - (prevOi.get(`${strike}|P`) ?? 0)
            : null,
          staleAge: inputs.staleAge[index],
          // Midpoint pro P/C v prémiích (#469); 0 = kotace chybí
          callMid: inputs.callMid[index],
          putMid: inputs.putMid[index],
          // Bez dat ≠ nula (#465): panel to kreslí šrafovaně
          callOiMissing: inputs.oiMissing.has(oiMissingKey(minuteKeys[minuteIdx], strike, 'C')),
          putOiMissing: inputs.oiMissing.has(oiMissingKey(minuteKeys[minuteIdx], strike, 'P')),
        }
      })
      // Striky mimo snapshot grid (#849): široký OI z tasty (#828). Nesou
      // JEN denní OI — kotace, greeks a volume pro ně neexistují, takže
      // `archiveOnly` říká UI, ať je odliší místo aby prázdno vypadalo jako
      // naměřená nula (týž princip jako oiMissing, #465).
      const archive = new Map<number, { call: number; put: number }>()
      for (const row of inputs.oiToday) {
        const entry = archive.get(row.strike) ?? { call: 0, put: 0 }
        if (row.right === 'C') entry.call = row.oi
        else entry.put = row.oi
        archive.set(row.strike, entry)
      }
      // Strike může být v gridu (měřil se někdy během dne), ale v TÉHLE minutě
      // mít OI = 0, protože ho snapshot nepokryl. OI je přitom denní hodnota,
      // takže archiv je pak pravda — bez toho počítá P/C přes stovky striků
      // s nulou a ukáže opak reality (naměřeno 0,66 místo 2,92).
      const known = new Set(strikes)
      for (const row of rows) {
        const arch = archive.get(row.strike)
        if (!arch) continue
        if (row.callOi === 0 && arch.call > 0) row.callOi = arch.call
        if (row.putOi === 0 && arch.put > 0) row.putOi = arch.put
      }
      const extra = new Map<number, { call: number; put: number }>()
      for (const [strike, oi] of archive) {
        if (known.has(strike)) continue
        extra.set(strike, oi)
      }
      for (const [strike, oi] of extra) {
        rows.push({
          strike,
          callVolComponent: 0,
          callOiComponent: 0,
          putVolComponent: 0,
          putOiComponent: 0,
          callVolume: 0,
          putVolume: 0,
          callOi: oi.call,
          putOi: oi.put,
          distanceFromSpot: Number.isFinite(spotAtMinute) ? strike - spotAtMinute : 0,
          callOiChange: null,
          putOiChange: null,
          staleAge: 0,
          archiveOnly: true,
        })
      }
      rows.sort((a, b) => a.strike - b.strike)
      profileCache.set(minuteIdx, rows)
      return rows
    },
  }

  // Dyn GEX profil per minuta (ADR-0009) — sparse pole indexované minuteIdx
  const gexProfile: (GexProfileRow | null)[] = Array.from({ length: minutes }, () => null)
  for (const row of inputs.gexProfile) {
    const minuteIdx = minuteIndex.get(row.tsIso)
    if (minuteIdx !== undefined) gexProfile[minuteIdx] = row
  }
  // FA varianta (#232) — stejný sparse tvar; bez řady zůstává null
  let gexProfileFa: (GexProfileRow | null)[] | null = null
  if (inputs.gexProfileFa.length > 0) {
    gexProfileFa = Array.from({ length: minutes }, () => null)
    for (const row of inputs.gexProfileFa) {
      const minuteIdx = minuteIndex.get(row.tsIso)
      if (minuteIdx !== undefined) gexProfileFa[minuteIdx] = row
    }
  }

  // GEX žebřík per minuta (#244) — stejný sparse vzor jako gexProfile
  const ladder: (LadderMinuteRow | null)[] = Array.from({ length: minutes }, () => null)
  for (const row of inputs.ladder) {
    const minuteIdx = minuteIndex.get(row.tsIso)
    if (minuteIdx !== undefined) ladder[minuteIdx] = row
  }

  // Popisek „od HH:MM" pro CumΔ (#518, ADR-0024): je-li PRVNÍ měřená minuta
  // dne catch-up, den nezačíná od začátku seance — tok před startem enginu
  // rekonstruovat nelze a panel to musí přiznat
  const firstMeasuredIdx = inputs.snapshotMinutes.findIndex((has) => has)
  const cumDeltaFromIso =
    firstMeasuredIdx >= 0 && isCatchUp(firstMeasuredIdx) ? minuteKeys[firstMeasuredIdx] : null

  return {
    symbol: inputs.symbol,
    expiry: inputs.expiry,
    date: inputs.date,
    minutes: minuteKeys,
    grid,
    raw,
    rawFa,
    overlays,
    panels: { vol, optVolCall, optVolPut, cumDelta, futuresCvd, deltaFlowCall, deltaFlowPut, evoOiCall, evoOiPut, cumDeltaFromIso }, // prettier-ignore
    profileByMinute,
    provisionalMinutes,
    gexProfile,
    gexField: inputs.gexField,
    gexProfileFa,
    gexFieldFa: inputs.gexFieldFa,
    ladder,
  }
}

/** Sestaví den v paměti z /replay balíku (exportováno kvůli testům). */
export function buildReplayDay(bundle: ReplayBundle): ReplayDay {
  return assembleReplayDay(decodeBundle(bundle))
}

/** Mapa „předchozí MĚŘENÁ minuta" pro přírůstkové panely (#459).
 *
 * `prev[i]` = index poslední minuty se snapshotem před `i`, nebo -1 (žádná —
 * první měřená minuta dne, nebo minuta bez snapshotu). Přírůstky se počítají
 * přes díru, ne vůči nulovému sloupci.
 *
 * Catch-up minuta (#518, ADR-0024) dostává `prev = -1` VŽDY: její kumulativy
 * dohánějí celou dobu výpadku, takže rozdíl proti čemukoli dřívějšímu není
 * minutový obchod. Pro NÁSLEDUJÍCÍ minuty ale předchůdcem je — její kumulativy
 * už jsou správné a diff proti nim je poctivá minuta. */
interface MeasuredMinutes {
  measured: (minuteIdx: number) => boolean
  prev: Int32Array
}

function measuredMinutes(
  snapshotMinutes: boolean[],
  minutes: number,
  isCatchUp?: (minuteIdx: number) => boolean,
): MeasuredMinutes {
  const measured = (minuteIdx: number): boolean => snapshotMinutes[minuteIdx] ?? true
  const prev = new Int32Array(minutes).fill(-1)
  let last = -1
  for (let minuteIdx = 0; minuteIdx < minutes; minuteIdx += 1) {
    if (!measured(minuteIdx)) continue
    prev[minuteIdx] = isCatchUp?.(minuteIdx) ? -1 : last
    last = minuteIdx
  }
  return { measured, prev }
}

/** Δ Flow per minuta: Σ přes strikes |delta| × kladný přírůstek volume (SPEC 4.6 váhy). */
function deltaFlowSeries(
  volume: Float32Array,
  delta: Float32Array,
  minutes: number,
  strikeCount: number,
  measured: MeasuredMinutes,
): number[] {
  const series = Array.from({ length: minutes }, () => 0)
  for (let strikeIdx = 0; strikeIdx < strikeCount; strikeIdx += 1) {
    for (let minuteIdx = 1; minuteIdx < minutes; minuteIdx += 1) {
      const previous = measured.prev[minuteIdx]
      if (previous < 0) continue
      const index = strikeIdx * minutes + minuteIdx
      const increment = volume[index] - volume[strikeIdx * minutes + previous]
      if (increment > 0) series[minuteIdx] += increment * Math.abs(delta[index])
    }
  }
  return series
}

/** OptVol per minuta: Σ kladných přírůstků kumulativního volume přes strikes. */
/** Σ OI přes striky per minuta (#573); minuta bez snapshotu = předchozí hodnota. */
export function oiTotalSeries(
  oi: Float32Array,
  minutes: number,
  strikeCount: number,
  hasSnapshot: (minuteIdx: number) => boolean,
): number[] {
  const series = Array.from({ length: minutes }, () => 0)
  for (let minuteIdx = 0; minuteIdx < minutes; minuteIdx += 1) {
    if (!hasSnapshot(minuteIdx)) {
      series[minuteIdx] = minuteIdx > 0 ? series[minuteIdx - 1] : 0
      continue
    }
    let total = 0
    for (let strikeIdx = 0; strikeIdx < strikeCount; strikeIdx += 1) {
      total += oi[strikeIdx * minutes + minuteIdx]
    }
    series[minuteIdx] = total
  }
  return series
}

function optVolSeries(
  volume: Float32Array,
  minutes: number,
  strikeCount: number,
  measured: MeasuredMinutes,
): number[] {
  const series = Array.from({ length: minutes }, () => 0)
  for (let strikeIdx = 0; strikeIdx < strikeCount; strikeIdx += 1) {
    for (let minuteIdx = 1; minuteIdx < minutes; minuteIdx += 1) {
      const previous = measured.prev[minuteIdx]
      if (previous < 0) continue
      const index = strikeIdx * minutes + minuteIdx
      const increment = volume[index] - volume[strikeIdx * minutes + previous]
      if (increment > 0) series[minuteIdx] += increment
    }
  }
  return series
}
