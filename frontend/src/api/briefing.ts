/** Ranní briefing (#674): REST klient + čisté skládací helpery.

Obrazovka je ČISTÁ KOMPOZICE existujících dat — žádný nový výpočet v enginu.
Fetchery čtou lehké endpointy (/bars, /oidelta, /levels, /gexforward,
/gammacliff, /news/upcoming, /sentiment/state, /briefing/verdicts — verdikt dne
počítá a ukládá engine, #1595); helpery z barů skládají
overnight rozsah a včerejší settle. US open se počítá DST-korektně přes
zonedTimeUtc (#511).
*/
import { API_BASE } from '../config'
import type { NewsExpectation, TurnLevel, TypicalReaction, VerdictKind, VerdictVote } from '../instrument/daysummary' // prettier-ignore
import { VERDICT_LABELS, pragueTime, verdictSummary } from '../instrument/daysummary'
import { isUsMarketHoliday } from '../instrument/holidays'
import { TIMEFRAMES } from '../instrument/trend'
import type { Candle, TimeframeKey, TrendReport } from '../instrument/trend'
import { isTradingSessionIso, zonedTimeUtc } from '../instrument/tz'

export interface BarRow {
  ts_min: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface LevelsRow {
  ts_min: string
  flip: number | null
  call_wall: number | null
  put_wall: number | null
  centroid: number | null
  total_gex: number
  /** Dominance zdí (ADR-0010, #1241) — chybí u starších partic. */
  call_wall_dom?: number | null
  put_wall_dom?: number | null
}

export interface OiDeltaSummary {
  symbol: string
  expiry: string
  days: { current: string; previous: string | null } | null
  call_total?: number
  put_total?: number
  call_delta?: number
  put_delta?: number
  movers?: Array<{ strike: number; right: 'C' | 'P'; oi: number; delta: number }>
}

export interface CliffToday {
  session_date: string
  cliff_share: number | null
  is_opex: boolean
}

async function getJson<T>(path: string, fallback: T): Promise<T> {
  try {
    const response = await fetch(`${API_BASE}${path}`)
    if (!response.ok) return fallback
    return (await response.json()) as T
  } catch {
    return fallback
  }
}

/** Svíčky vyššího timeframe (#1089): `GET /candles/{sym}?tf=&limit=` — D = Globex
seance, W z denních, intradenní koše od otevření seance; poslední může být `partial`. */
export async function fetchCandles(
  symbol: string,
  tf: TimeframeKey,
  limit: number,
): Promise<Candle[]> {
  // prettier-ignore
  const data = await getJson<{ candles: Candle[] }>(`/candles/${symbol}?tf=${tf}&limit=${limit}`, {
    candles: [],
  })
  return data.candles
}

/** Kolik svíček per timeframe stáhnout: EMA50 + rezerva na pivoty; týden má
z 2 let barů ~100 svíček, intradenní koše jen pár seancí (limit endpointu 600).
Délka okna mění EMA (start z průměru prvních svíček) — tabulka je táž jako
v enginu `scenario_auto.CANDLE_LIMITS`, ať karta Trend a hlasy trendu v uloženém
verdiktu čtou stejné svíčky (#1595); mění se spolu. */
export const CANDLE_LIMITS: Record<TimeframeKey, number> = {
  W: 80,
  D: 120,
  '240': 120,
  '60': 120,
  '15': 120,
}

/** Stáhne svíčky všech timeframů; chybějící TF (chyba, 404) nechá prázdný. */
export async function fetchCandlesByTimeframe(
  symbol: string,
): Promise<Partial<Record<TimeframeKey, Candle[]>>> {
  const pairs = await Promise.all(
    TIMEFRAMES.map(async (tf) => [tf, await fetchCandles(symbol, tf, CANDLE_LIMITS[tf])] as const),
  )
  const result: Partial<Record<TimeframeKey, Candle[]>> = {}
  for (const [tf, candles] of pairs) if (candles.length > 0) result[tf] = candles
  return result
}

/** Typická reakce per kategorie (#1090): medián |ret_bp| měřených reakcí v oknech. */
export async function fetchTypicalReactions(symbol: string): Promise<TypicalReaction[]> {
  const data = await getJson<{ typical: TypicalReaction[] }>(
    `/news/reactions/typical?symbol=${symbol}`,
    { typical: [] },
  )
  return data.typical
}

/** Uložený verdikt dne (#1595) — řádek `briefing_verdicts`, zapisuje ho jen engine.
Jen sloupce, které Briefing čte; `outcome_*` patří do Stats (#1091). */
export interface StoredVerdict {
  session_date: string
  verdict: VerdictKind
  score: number
  votes: VerdictVote[]
  /** Poslední zápis enginu (ISO UTC) — stáří předběžného verdiktu. */
  updated_at: string
}

/** Výsledek čtení uloženého verdiktu: řádek seance (null = engine ho nezapsal), nebo chyba. */
export type StoredVerdictResult = { ok: true; row: StoredVerdict | null } | { ok: false; error: string } // prettier-ignore

const DAY_MS = 86_400_000
/** Strop `days` endpointu `GET /briefing/verdicts`. */
const VERDICTS_MAX_DAYS = 400

function verdictLoadError(detail: string): StoredVerdictResult {
  return { ok: false, error: `Verdikt se nepodařilo načíst (${detail})` }
}

function isStoredVerdict(row: Partial<StoredVerdict>): row is StoredVerdict {
  return (
    typeof row.verdict === 'string' &&
    Object.keys(VERDICT_LABELS).includes(row.verdict) &&
    typeof row.score === 'number' &&
    Array.isArray(row.votes) &&
    typeof row.updated_at === 'string' &&
    Number.isFinite(Date.parse(row.updated_at))
  )
}

/** Uložený verdikt seance `dateIso` (#1595): `GET /briefing/verdicts?symbol=&days=`.

Na rozdíl od `getJson` chyby NEpolyká: výpadek API nebo DB není „verdikt
nevznikl" — karta ukáže chybu s kódem. API filtruje od dnešního UTC data − `days`,
proto `days` pokryje i starší prohlížené datum (strop 400). */
export async function fetchStoredVerdict(
  symbol: string,
  dateIso: string,
  nowMs: number = Date.now(),
): Promise<StoredVerdictResult> {
  const todayUtc = Date.parse(new Date(nowMs).toISOString().slice(0, 10))
  const back = Math.ceil((todayUtc - Date.parse(dateIso)) / DAY_MS) + 1
  const days = Math.min(VERDICTS_MAX_DAYS, Math.max(1, back))
  const query = new URLSearchParams({ symbol, days: String(days) })
  let response: Response
  try {
    response = await fetch(`${API_BASE}/briefing/verdicts?${query.toString()}`)
  } catch (error) {
    return verdictLoadError(`síť: ${error instanceof Error ? error.message : String(error)}`)
  }
  if (!response.ok) return verdictLoadError(`HTTP ${response.status}`)
  let data: { verdicts?: unknown }
  try {
    data = (await response.json()) as { verdicts?: unknown }
  } catch {
    return verdictLoadError('odpověď není JSON')
  }
  if (!Array.isArray(data.verdicts)) return verdictLoadError('odpověď bez seznamu verdiktů')
  const rows = data.verdicts as Array<Partial<StoredVerdict>>
  const row = rows.find((item) => item.session_date === dateIso)
  if (row === undefined) return { ok: true, row: null }
  return isStoredVerdict(row)
    ? { ok: true, row }
    : verdictLoadError(`neplatný řádek seance ${dateIso}`)
}

/** Track record verdiktů dne (#1091): hit-rate per verdikt a per složka, Wilson LB, brána n ≥ 30. */
export interface VerdictStatBucket {
  n: number
  hits: number
  unscored?: number
  hit_rate: number | null
  wilson_lb: number | null
  gate_open: boolean
}

export interface VerdictStats {
  evaluated: number
  min_samples: number
  by_verdict: Record<string, VerdictStatBucket>
  by_vote: Record<string, VerdictStatBucket>
}

export async function fetchVerdictStats(symbol?: string): Promise<VerdictStats> {
  const query = symbol ? `?symbol=${symbol}` : ''
  const data = await getJson<Partial<VerdictStats>>(`/briefing/verdicts/stats${query}`, {})
  // Tvar se drží i při neúplné odpovědi (starší API, výpadek) — sekce nesmí shodit Stats
  return {
    evaluated: typeof data.evaluated === 'number' ? data.evaluated : 0,
    min_samples: typeof data.min_samples === 'number' ? data.min_samples : 30,
    by_verdict: data.by_verdict ?? {},
    by_vote: data.by_vote ?? {},
  }
}

export async function fetchBars(symbol: string, dateIso: string): Promise<BarRow[]> {
  const data = await getJson<{ bars: BarRow[] }>(`/bars/${symbol}?date=${dateIso}`, { bars: [] })
  return data.bars
}

export async function fetchLevelsSeries(
  symbol: string,
  expiry: string,
  dateIso: string,
): Promise<LevelsRow[]> {
  const data = await getJson<{ levels: LevelsRow[] }>(
    `/levels/${symbol}/${expiry}?date=${dateIso}`,
    { levels: [] },
  )
  return data.levels
}

export async function fetchOiDelta(symbol: string, expiry: string): Promise<OiDeltaSummary> {
  return getJson<OiDeltaSummary>(`/oidelta/${symbol}/${expiry}`, {
    symbol,
    expiry,
    days: null,
  })
}

export async function fetchCliffToday(symbol: string): Promise<CliffToday | null> {
  const data = await getJson<{ today: CliffToday | null }>(`/gammacliff/${symbol}`, {
    today: null,
  })
  return data.today
}

/** Útes gammy poslední UZAVŘENÉ seance (#1241): řádek tabulky, ne živý odhad. */
export interface CliffPrevious {
  session_date: string
  cliff_share: number | null
  is_opex: boolean
}

export async function fetchCliffPrevious(symbol: string): Promise<CliffPrevious | null> {
  const data = await getJson<{ rows: CliffPrevious[] }>(`/gammacliff/${symbol}?limit=1`, {
    rows: [],
  })
  return data.rows[0] ?? null
}

export async function fetchStoredDays(symbol: string): Promise<string[]> {
  const data = await getJson<{ days: Array<{ date: string }> }>(`/instruments/${symbol}/days`, {
    days: [],
  })
  return data.days.map((day) => day.date)
}

/** Řádek /volregime — volatilitní režim seance (ADR-0028, #713). */
export interface VolRegimeRow {
  session_date: string
  session_range: number
  percentile: number
  bucket: string
  sample: number
}

/** České popisky bucketů vol režimu (ADR-0028) pro kartu i plán. */
export const VOL_BUCKET_LABELS: Record<string, string> = {
  low: 'nízká',
  normal: 'normální',
  elevated: 'zvýšená',
  crisis: 'krizová',
}

/** Poslední spočtená seance vol režimu; null = málo vzorků nebo engine neběžel. */
export async function fetchVolRegimeLatest(symbol: string): Promise<VolRegimeRow | null> {
  const data = await getJson<{ rows?: VolRegimeRow[] }>(`/volregime/${symbol}?limit=1`, {
    rows: [],
  })
  const rows = data.rows ?? []
  return rows.length > 0 ? rows[0] : null
}

/** Řádek /ivrank — denní IV s kontextem per zdroj (#871). */
export interface IvRankRow {
  session_date: string
  source: string
  iv: number
  iv_rank: number | null
  iv_percentile: number | null
  sample: number
}

/** Poslední řádky /ivrank per zdroj (ibkr/tasty/own_atm); prázdné = bez dat. */
export async function fetchIvRankLatest(symbol: string): Promise<IvRankRow[]> {
  const data = await getJson<{ latest?: IvRankRow[] }>(`/ivrank/${symbol}?limit=1`, {
    latest: [],
  })
  return data.latest ?? []
}

/** Primární IVR čtení (rozhodnutí uživatele 26. 8.): percentil z řady `ibkr`
(plná roční historie, transparentní konstrukce); rank a tasty křížová
kontrola patří do tooltipu. Null = řada chybí nebo je pod MIN_SAMPLE. */
export function ivRankPrimary(rows: IvRankRow[]): IvRankRow | null {
  const ibkr = rows.find((row) => row.source === 'ibkr')
  return ibkr && ibkr.iv_percentile !== null ? ibkr : null
}

/** Tooltip IVR: rank + tasty kontrola — vše, co se do řádku nevešlo. */
export function ivRankTooltip(rows: IvRankRow[]): string {
  // Nativní title respektuje odřádkování — pásma pod sebou, ne jeden odstavec (27. 8.)
  const parts: string[] = [
    'IV percentil = podíl dnů v klouzavém roce s nižší 30d IV podkladu (řada IBKR):',
    'p1 = jen 1 % dnů za rok mělo IV níž. Neříká směr — jen kolik pohybu trh oceňuje.',
    '',
    'Orientační pásma (vodítko, ne signál):',
    '• p0–20 — prémie levné, malý očekávaný pohyb (úzké EM; klid umí podcenit riziko)',
    '• p20–50 — běžné pásmo',
    '• p50–80 — zvýšené očekávání, prémie dražší',
    '• p80–100 — drahá prémie, stres kolem událostí (široké EM)',
    '',
  ]
  const ibkr = rows.find((row) => row.source === 'ibkr')
  if (ibkr?.iv_rank != null) {
    parts.push(`IV Rank (poloha mezi ročním min–max): ${Math.round(ibkr.iv_rank * 100)}.`)
  }
  const tasty = rows.find((row) => row.source === 'tasty')
  if (tasty) {
    const pct = tasty.iv_percentile != null ? ` p${Math.round(tasty.iv_percentile * 100)}` : ''
    parts.push(
      `Křížová kontrola tasty: IV ${(100 * tasty.iv).toFixed(1)} %${pct} — jiná konstrukce, čísla se záměrně nemíchají.`,
    )
  }
  return parts.join('\n')
}

/** Rich/cheap prémie (#875, D5): spread percentilů implied − realized.

Heuristika, ne měření: obě strany jsou percentily RŮZNÝCH veličin (30d IV
index z #871 vs. rozsah seance z ADR-0028) — spread říká, jestli trh platí
za pohyb víc, než kolik se ho reálně děje. Práh ±20 p. b. je vědomá volba
(menší rozdíl je šum percentilů), zdokumentováno v manuálu. */
export const PREMIUM_SPREAD_THRESHOLD = 0.2

export interface PremiumReading {
  /** IV percentil − HV percentil, −1..1. */
  spread: number
  label: 'rich' | 'neutral' | 'cheap'
  ivPercentile: number
  hvPercentile: number
}

/** Null = chybí IVR nebo vol režim — žádný default (AC #875). */
export function premiumReading(
  ivRows: IvRankRow[],
  vol: VolRegimeRow | null,
): PremiumReading | null {
  const primary = ivRankPrimary(ivRows)
  if (primary?.iv_percentile == null || vol === null) return null
  const spread = primary.iv_percentile - vol.percentile
  // Epsilon: p60 − p40 je ve floatech 0,19999… a hrana prahu by uhýbala
  const eps = 1e-9
  const label =
    spread >= PREMIUM_SPREAD_THRESHOLD - eps
      ? 'rich'
      : spread <= -(PREMIUM_SPREAD_THRESHOLD - eps)
        ? 'cheap'
        : 'neutral'
  return { spread, label, ivPercentile: primary.iv_percentile, hvPercentile: vol.percentile }
}

/** Text řádku: verdikt + JEN hodnocené číslo (spread). IV/HV percentily
řádek neopakuje — IV má vlastní řádek výš, HV nese řádek Režim a rozklad
spreadu je v tooltipu (duplikace vytýkána 27. 8.). */
export function premiumLabel(reading: PremiumReading): string {
  const spreadPb = Math.round(100 * reading.spread)
  const spread = `spread ${spreadPb >= 0 ? '+' : ''}${spreadPb} p. b.`
  if (reading.label === 'rich') return `rich: ${spread} — trh platí za hedge`
  if (reading.label === 'cheap') return `cheap: ${spread} — trh pohyb podceňuje`
  return `neutrální: ${spread}`
}

/** Tooltip prémie — odřádkovaný (pravidlo 27. 8.), kontext dne, ne signál. */
export function premiumTooltip(reading: PremiumReading): string {
  const spreadPb = Math.round(100 * reading.spread)
  const ivP = Math.round(100 * reading.ivPercentile)
  const hvP = Math.round(100 * reading.hvPercentile)
  return [
    'Rich/cheap hodnotí JEDINÉ číslo — spread = IV percentil − HV percentil.',
    `Dnes: p${ivP} − p${hvP} = ${spreadPb >= 0 ? '+' : ''}${spreadPb} p. b. → ${reading.label === 'neutral' ? 'neutrální' : reading.label}.`,
    '',
    'Pásma platí pro tento spread (NE pro IV ani HV samotné):',
    '• rich ≥ +20 p. b. — trh platí za pohyb víc, než kolik se hýbe;',
    '  typicky intenzivnější dealer hedging flow kolem zdí',
    '• neutrální mezi −20 a +20 p. b.',
    '• cheap ≤ −20 p. b. — prémie levná vůči reálnému rozsahu (pozor u průrazů)',
    '',
    'IV percentil = co trh do budoucna OCEŇUJE (řádek IV percentil výše).',
    'HV percentil = co se reálně DĚJE (percentil z řádku Režim).',
    'Heuristika — percentily různých veličin; kontext dne, ne signál.',
  ].join('\n')
}

/** Souhrn /emrespect — jak často close končí uvnitř pásma EM (#872). */
export interface EmRespectSummary {
  window_days: number
  n: number
  close_in_band_share: number
  touch_upper_share: number
  touch_lower_share: number
}

export async function fetchEmRespectSummary(symbol: string): Promise<EmRespectSummary | null> {
  const data = await getJson<{ summary?: EmRespectSummary | null }>(
    `/emrespect/${symbol}?limit=1`,
    { summary: null },
  )
  return data.summary ?? null
}

/** US open (9:30 New York) daného dne v epoch ms — DST řeší zoneinfo (#511). */
export function usOpenMs(dateIso: string): number {
  const [year, month, day] = dateIso.split('-').map(Number)
  return zonedTimeUtc('America/New_York', year, month, day, 9, 30)
}

/** Od kolika minut před US openem engine přepočítává verdikt každou minutu —
zrcadlo enginu `verdict_writer.VERDICT_WINDOW_MINUTES` (#1595), mění se spolu. */
export const VERDICT_EVERY_MINUTE_BEFORE_OPEN = 15

/** Stav karty Verdikt dne (#1595) — Briefing jen ukazuje, co uložil engine. */
export interface VerdictCard {
  kind:
    | 'no_session' // víkend nebo svátek NYSE — verdikt se nevydává
    | 'no_pipeline' // symbol bez pipeline enginu — verdikt nikdo nepočítá
    | 'error' // čtení selhalo — výslovná chyba, ne „bez verdiktu"
    | 'loading' // první odpověď ještě nedorazila
    | 'preliminary' // uložený řádek před US openem — engine ho ještě přepisuje
    | 'frozen' // uložený řádek od US openu — už se nemění
    | 'pending' // bez řádku před openem — vznikne
    | 'missing' // bez řádku po openu — viditelná mezera, nic se nedopočítává
  /** Stavová věta karty; u uloženého řádku jeho platnost a stáří. */
  message: string
  row: StoredVerdict | null
}

const SESSION_DAY = new Intl.DateTimeFormat('cs-CZ', {
  timeZone: 'UTC',
  weekday: 'long',
  day: 'numeric',
  month: 'numeric',
})

/** Stáří zápisu: „před 3 min", „před 2 h 5 min". */
function ageText(ms: number): string {
  const minutes = Math.floor(Math.max(0, ms) / 60_000)
  if (minutes < 1) return 'před chvílí'
  if (minutes < 60) return `před ${minutes} min`
  return `před ${Math.floor(minutes / 60)} h ${minutes % 60} min`
}

/** Čistý stav karty verdiktu z uloženého řádku, obchodního dne a pipeline (#1595).

`pipelineSymbols` je `status.symbols` enginu (čárkami); bez něj (engine status
zatím nedorazil) se o pipeline nic netvrdí. `stored` null = načítá se. Časy
v pražském čase jako zbytek Briefingu. */
export function verdictCard(input: {
  dateIso: string
  symbol: string
  pipelineSymbols: string | undefined
  stored: StoredVerdictResult | null
  nowMs: number
}): VerdictCard {
  const { dateIso, symbol, pipelineSymbols, stored, nowMs } = input
  if (!isTradingSessionIso(dateIso)) {
    const day = `${SESSION_DAY.format(new Date(`${dateIso}T12:00:00Z`))}${isUsMarketHoliday(dateIso) ? ' (svátek NYSE)' : ''}` // prettier-ignore
    return { kind: 'no_session', message: `Verdikt se nevydává — ${day} není obchodní seance.`, row: null } // prettier-ignore
  }
  // Uložený řádek má přednost: symbol mohl pipeline ztratit až po zápisu (watchlist)
  const hasRow = stored !== null && stored.ok && stored.row !== null
  if (!hasRow && pipelineSymbols !== undefined && !pipelineSymbols.split(',').includes(symbol)) {
    return { kind: 'no_pipeline', message: 'Engine pro tento symbol verdikt nepočítá.', row: null }
  }
  if (stored === null) return { kind: 'loading', message: 'Verdikt se načítá.', row: null }
  if (!stored.ok) return { kind: 'error', message: stored.error, row: null }
  const openMs = usOpenMs(dateIso)
  const beforeOpen = nowMs < openMs
  const { row } = stored
  if (row === null) {
    return beforeOpen
      ? { kind: 'pending', message: 'Verdikt zatím nevznikl — engine ho ukládá od začátku seance každých 5 min.', row: null } // prettier-ignore
      : { kind: 'missing', message: 'Verdikt pro tuto seanci nevznikl.', row: null }
  }
  const savedAt = pragueTime(row.updated_at)
  if (!beforeOpen) return { kind: 'frozen', message: `zmrazený — poslední zápis ${savedAt}`, row }
  const everyMinuteFrom = pragueTime(new Date(openMs - VERDICT_EVERY_MINUTE_BEFORE_OPEN * 60_000).toISOString()) // prettier-ignore
  const open = pragueTime(new Date(openMs).toISOString())
  return {
    kind: 'preliminary',
    message: `předběžný: engine ho přepočítává každých 5 min, od ${everyMinuteFrom} každou minutu; platí poslední stav před openem ${open}. Uloženo ${savedAt}, ${ageText(nowMs - Date.parse(row.updated_at))}.`, // prettier-ignore
    row,
  }
}

export interface RangeSummary {
  high: number
  low: number
  last: number
  lastTs: string
}

/** Extrémy a poslední close z barů; volitelně jen do okamžiku `untilMs`. */
export function barsRange(bars: BarRow[], untilMs?: number): RangeSummary | null {
  let summary: RangeSummary | null = null
  for (const bar of bars) {
    if (untilMs !== undefined && Date.parse(bar.ts_min) >= untilMs) continue
    if (summary === null) {
      summary = { high: bar.high, low: bar.low, last: bar.close, lastTs: bar.ts_min }
    } else {
      summary.high = Math.max(summary.high, bar.high)
      summary.low = Math.min(summary.low, bar.low)
      summary.last = bar.close
      summary.lastTs = bar.ts_min
    }
  }
  return summary
}

/** Poslední řádek levels řady — aktuální flip/walls/total_gex briefingu. */
export function latestLevels(rows: LevelsRow[]): LevelsRow | null {
  return rows.length > 0 ? rows[rows.length - 1] : null
}

/** Poslední uložený den PŘED `dateIso` — včerejší seance pro settle/PDH/PDL. */
export function previousStoredDay(days: string[], dateIso: string): string | null {
  const before = days.filter((day) => day < dateIso).sort()
  return before.length > 0 ? before[before.length - 1] : null
}

/** Režim gammy pro briefing: znaménko total_gex + poloha spotu vůči flipu. */
export function gammaRegimeLabel(levels: LevelsRow | null, spot: number | null): string {
  if (levels === null) return 'bez dat'
  const sign = levels.total_gex >= 0 ? 'pozitivní gamma (pohyb se tlumí)' : 'negativní gamma (pohyb se zesiluje)' // prettier-ignore
  if (spot === null || levels.flip === null) return sign
  const side = spot >= levels.flip ? 'nad flipem' : 'pod flipem'
  return `${sign}, cena ${side}`
}

/** EM pro plán/kartu (#873) — jen čísla, výpočet dělá instrument/expectedmove. */
export interface PlanEm {
  em: number
  anchor: number
  preOpen: boolean
}

/** Řádek „Volatilita" (#873): vol režim + EM; bez obou poctivé „bez dat". */
export function volatilityLine(vol: VolRegimeRow | null, em: PlanEm | null): string {
  const parts: string[] = []
  if (vol) {
    const label = VOL_BUCKET_LABELS[vol.bucket] ?? vol.bucket
    parts.push(`${label} (p${Math.round(vol.percentile * 100)}, ${vol.sample} seancí)`)
  }
  if (em) {
    const pct = (100 * em.em) / em.anchor
    parts.push(`EM ±${em.em.toFixed(1)} b (${pct.toFixed(2)} %${em.preOpen ? ', pre-open odhad' : ''})`) // prettier-ignore
  }
  return parts.length > 0 ? parts.join(' · ') : 'bez dat (málo vzorků nebo chybí straddle)'
}

/** Text ranního plánu do deníku (#673) — předvyplněná kostra z briefingu. */
export function briefingToPlanText(input: {
  symbol: string
  regime: string
  levels: LevelsRow | null
  overnight: RangeSummary | null
  prevDay: RangeSummary | null
  cliff: CliffToday | null
  /** Volatility box (#873): vol režim + EM; undefined = data zatím nedorazila. */
  vol?: VolRegimeRow | null
  em?: PlanEm | null
  /** Shrnutí dne (#1090): trend, verdikt, úrovně obratu, zprávy — jen když jsou.
  Verdikt jen z uloženého řádku enginu (#1595); bez řádku plán verdikt vynechá. */
  trend?: TrendReport | null
  verdict?: VerdictCard | null
  turnLevels?: TurnLevel[]
  news?: NewsExpectation[]
}): string {
  const lines: string[] = [`Plán dne ${input.symbol}:`, `- Režim: ${input.regime}`]
  const { levels } = input
  if (levels) {
    const fmt = (value: number | null) => (value === null ? '—' : String(value))
    lines.push(
      `- Úrovně: flip ${fmt(levels.flip)}, call wall ${fmt(levels.call_wall)}, put wall ${fmt(levels.put_wall)}`,
    )
  }
  // Volatility box (#873): vědomé potvrzení režimu patří do každého plánu —
  // řádek s hodnotami jen když jsou, checkbox vždy (rituál, ne data)
  lines.push(`- Volatilita: ${volatilityLine(input.vol ?? null, input.em ?? null)}`)
  lines.push('- [ ] riziko přizpůsobeno režimu (stop/velikost)')
  if (input.prevDay) lines.push(`- Včera: settle ${input.prevDay.last}, rozsah ${input.prevDay.low}–${input.prevDay.high}`) // prettier-ignore
  if (input.overnight) lines.push(`- Overnight: ${input.overnight.low}–${input.overnight.high}, teď ${input.overnight.last}`) // prettier-ignore
  if (input.cliff?.cliff_share != null) {
    const pct = Math.round(input.cliff.cliff_share * 100)
    lines.push(`- Dnes odpadá ~${pct} % gammy${input.cliff.is_opex ? ' (OPEX!)' : ''}`)
  }
  // Shrnutí dne (#1090): trend shora dolů, úrovně obratu, zprávy s časem, verdikt
  if (input.trend && input.trend.decided > 0) lines.push(`- Trend: ${input.trend.reading}`)
  const nearest = (input.turnLevels ?? []).slice(0, 6)
  if (nearest.length > 0) {
    lines.push(
      `- Úrovně obratu: ${nearest.map((level) => `${level.label} ${level.price} (${level.role})`).join(', ')}`,
    )
  }
  const news = (input.news ?? []).filter((item) => item.highImpact).slice(0, 4)
  if (news.length > 0) {
    lines.push(
      `- Zprávy: ${news.map((item) => `${item.timeLabel} ${item.row.title} — ${item.magnitude}`).join('; ')}`,
    )
  }
  const stored = input.verdict?.row
  if (stored) {
    // Plán je záznam okamžiku: předběžný verdikt se do openu ještě může změnit
    const savedAt = pragueTime(stored.updated_at)
    const validity = input.verdict?.kind === 'frozen' ? `zmrazený, poslední zápis ${savedAt}` : `předběžný, uloženo ${savedAt}` // prettier-ignore
    lines.push(`- Verdikt dne: ${verdictSummary(stored.verdict, stored.score)} (${validity})`)
  }
  lines.push('- Teze dne: ')
  return lines.join('\n')
}
