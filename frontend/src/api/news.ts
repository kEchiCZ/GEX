/** SentimentLens (SPEC kap. 8): REST klient pro zprávy a index sentimentu. */
import { API_BASE } from '../config'

export interface NewsRow {
  id: number
  ts_event: string
  ts_ingested: string
  source: string
  kind: 'scheduled' | 'headline' | 'social' | 'broker'
  category: string | null
  importance: number | null
  /** Surový záznam zdroje (ForexFactory) — nese `impact` (High/Medium/Low).
  `importance` na to není spolehlivý proxy: hodnota 3 se v datech objevuje
  i u Low událostí (#830). */
  raw?: Record<string, unknown> | null
  title: string
  summary: string | null
  sentiment_dir: number | null
  /** `Numeric` sloupce můžou z API dorazit jako řetězec (PG Decimal). */
  sentiment_score: number | string | null
  sentiment_source: string | null
  forecast: number | string | null
  previous: number | string | null
  /** Naměřený dopad (#656): párovací okno (min) → ret_bp pro symbol feedu;
  null/undefined = okna ještě neuzavřená nebo bez reakce. */
  reactions_bp?: Record<string, number | string> | null
  /** Do některého okna spadl jiný významný event — pohyb nejde přičíst téhle
  zprávě (SPEC 5.1); karta to značí ⚠. */
  reaction_contaminated?: boolean
  /** Index tématu k okamžiku zprávy (#656 bod 5) — kontext narativu; den
  a týden u našich poločasů (≤ 6 h) splývají, proto jedna hodnota. */
  topic_value?: number | null
  actual: number | string | null
}

export interface SentimentPoint {
  ts_min: string
  value: number
}

export interface TopicRow {
  category: string
  value: number
  events_in_window: number
  active: boolean
}

/** České popisky kategorií — v UI se nikde nemají ukazovat konstanty z DB. */
export const CATEGORY_LABELS: Record<string, string> = {
  FED: 'Fed',
  MACRO_INFLATION: 'Inflace',
  MACRO_LABOR: 'Trh práce',
  MACRO_GROWTH: 'Růst',
  GEOPOLITICS: 'Geopolitika',
  ENERGY: 'Energie',
  TECH: 'Technologie',
  EARNINGS: 'Výsledky',
  CRYPTO: 'Krypto',
  OTHER: 'Ostatní',
}

/** Glyf kategorie pro marker i řádek feedu (SPEC 9.1). */
export const CATEGORY_GLYPHS: Record<string, string> = {
  FED: '🏛',
  MACRO_INFLATION: '📊',
  MACRO_LABOR: '👷',
  MACRO_GROWTH: '📈',
  GEOPOLITICS: '⚡',
  ENERGY: '🛢',
  TECH: '💻',
  EARNINGS: '💰',
  CRYPTO: '₿',
  OTHER: '•',
}

export function categoryLabel(category: string | null): string {
  if (!category) return 'Nezařazeno'
  return CATEGORY_LABELS[category] ?? category
}

export function categoryGlyph(category: string | null): string {
  if (!category) return '•'
  return CATEGORY_GLYPHS[category] ?? '•'
}

/** Odstup do události v lidské podobě: `za 1 h 12 m`, `za 8 m`, `právě teď`. */
export function countdownLabel(target: string, now: Date = new Date()): string {
  const minutes = Math.round((new Date(target).getTime() - now.getTime()) / 60000)
  if (minutes <= 0) return 'právě teď'
  if (minutes < 60) return `za ${minutes} m`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest === 0 ? `za ${hours} h` : `za ${hours} h ${rest} m`
}

/** Naváže řadu sentimentu na časovou osu grafu.

Index počítá news-engine nezávisle na tom, kdy dorazily bary, takže se hodnoty
párují podle času, ne podle pořadí. Minuty bez hodnoty drží poslední známou —
index je spojitý, ne vzorkovaný — a před první hodnotou je nula.

`formatLabel` **musí být tentýž formatter, který vyrobil `labels`**. Vlastní
formátování by se s ním rozešlo (osa používá Intl bez vodicí nuly: `3:21`,
ne `03:21`) a řada by tiše vyšla samá nula. */
export function alignSeriesToLabels(
  series: SentimentPoint[],
  labels: string[],
  formatLabel: (iso: string) => string,
): number[] {
  if (labels.length === 0) return []
  const byLabel = new Map<string, number>()
  for (const point of series) {
    byLabel.set(formatLabel(point.ts_min), point.value)
  }
  const out: number[] = []
  let last = 0
  for (const label of labels) {
    const value = byLabel.get(label)
    if (value !== undefined) last = value
    out.push(last)
  }
  return out
}

async function getJson<T>(path: string, fallback: T): Promise<T> {
  try {
    const response = await fetch(`${API_BASE}${path}`)
    if (!response.ok) return fallback
    return (await response.json()) as T
  } catch {
    // API neběží — UI drží poslední známý stav místo pádu
    return fallback
  }
}

export async function fetchNews(limit = 100, symbol = 'ES'): Promise<NewsRow[]> {
  const data = await getJson<{ news: NewsRow[] }>(`/news?limit=${limit}&symbol=${symbol}`, {
    news: [],
  })
  return data.news
}

/** Hlavní naměřený dopad karty (#656): preferuje 5m okno, jinak nejkratší
uzavřené; null = žádná reakce ještě není. */
export function primaryReaction(row: NewsRow): { windowMin: number; bp: number } | null {
  const reactions = row.reactions_bp
  if (!reactions) return null
  const windows = Object.keys(reactions)
    .map(Number)
    .filter((value) => Number.isFinite(value))
    .sort((a, b) => a - b)
  if (windows.length === 0) return null
  const windowMin = windows.includes(5) ? 5 : windows[0]
  const raw = reactions[String(windowMin)]
  const bp = typeof raw === 'number' ? raw : Number(raw)
  return Number.isFinite(bp) ? { windowMin, bp } : null
}

/** Relativní stáří zprávy: „před 16 s / před 3 min / před 2 h“, starší = čas. */
export function relativeAge(iso: string, nowMs: number): string {
  const delta = Math.max(0, nowMs - Date.parse(iso))
  if (delta < 60_000) return `před ${Math.round(delta / 1000)} s`
  if (delta < 3_600_000) return `před ${Math.round(delta / 60_000)} min`
  if (delta < 86_400_000) return `před ${Math.round(delta / 3_600_000)} h`
  return new Date(iso).toLocaleString([], {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** Je to událost, kvůli které se hýbe celý trh? (#830)

Bere `impact` ze zdroje, `importance` jen jako záložku pro starší záznamy
bez `raw` — samo o sobě totiž rozlišuje špatně. */
export function isHighImpact(row: NewsRow): boolean {
  const impact = row.raw?.impact
  if (typeof impact === 'string') return impact.toLowerCase() === 'high'
  return (row.importance ?? 0) >= 3
}

export async function fetchUpcoming(hours = 24): Promise<NewsRow[]> {
  const data = await getJson<{ upcoming: NewsRow[] }>(`/news/upcoming?hours=${hours}`, {
    upcoming: [],
  })
  return data.upcoming
}

export async function fetchSentimentSeries(
  symbol: string,
  date?: string,
): Promise<SentimentPoint[]> {
  const query = date ? `?date=${date}` : ''
  const data = await getJson<{ series: SentimentPoint[] }>(`/sentiment/index/${symbol}${query}`, {
    series: [],
  })
  return data.series
}

export async function fetchTopics(activeOnly = false): Promise<TopicRow[]> {
  const data = await getJson<{ topics: TopicRow[] }>(
    `/sentiment/topics${activeOnly ? '?active=1' : ''}`,
    { topics: [] },
  )
  return data.topics
}

/** Téma v čase (#566): řada kumulativního indexu + podíl na období. */
export interface TopicSeriesRow {
  category: string
  events: number
  weight: number
  /** Podíl na Σ vah období (0–1) — „co trh zrovna řeší". */
  share: number
  points: { ts: string; value: number }[]
}

export async function fetchTopicSeries(days: number): Promise<TopicSeriesRow[]> {
  const data = await getJson<{ topics: TopicSeriesRow[] }>(
    `/sentiment/topics/series?days=${days}`,
    { topics: [] },
  )
  return data.topics
}

/** Zpráva tvořící téma (#566 fáze 3) — dohledatelnost hodnoty indexu. */
export interface TopicEventRow {
  id: number
  ts_event: string
  title: string
  source: string
  importance: number | null
  sentiment_dir: number | null
  sentiment_score: number | null
}

export async function fetchTopicEvents(category: string, days: number): Promise<TopicEventRow[]> {
  const data = await getJson<{ events: TopicEventRow[] }>(
    `/sentiment/topics/${category}/events?days=${days}`,
    { events: [] },
  )
  return data.events
}

/** Bod crowd řady (#290, SPEC 2.6) — F&G skóre, PCR, Reddit průměry. */
export interface CrowdRow {
  ts: string
  source: string
  metric: string
  symbol: string
  value: number | string
  raw: Record<string, unknown> | null
}

export async function fetchCrowd(): Promise<CrowdRow[]> {
  const data = await getJson<{ crowd: CrowdRow[] }>('/sentiment/crowd', { crowd: [] })
  return data.crowd
}

/** Poslední bod každé řady (source|metric|symbol) — pro souhrnný blok v News. */
export function latestCrowd(rows: CrowdRow[]): Map<string, CrowdRow> {
  const latest = new Map<string, CrowdRow>()
  for (const row of rows) {
    const key = `${row.source}|${row.metric}|${row.symbol}`
    const existing = latest.get(key)
    if (!existing || existing.ts < row.ts) latest.set(key, row)
  }
  return latest
}

/** Denní OHLC svíčka SentIndexu (#296, SPEC 7.1) — open nese overnight zbytek. */
export interface SentimentDailyRow {
  date: string
  symbol: string
  open: number
  high: number
  low: number
  close: number
}

export async function fetchSentimentDaily(
  symbol: string,
  fromDate?: string,
): Promise<SentimentDailyRow[]> {
  const from = fromDate ? `&from=${fromDate}` : ''
  const data = await getJson<{ daily: SentimentDailyRow[] }>(
    `/sentiment/daily?symbol=${symbol}${from}`,
    { daily: [] },
  )
  return data.daily ?? []
}

/** Stav RiskOn/RiskOff/Neutral (#292/#295, SPEC 5.6 a 9.0). */
export interface SentimentStateInfo {
  symbol: string
  state: 'RiskOn' | 'RiskOff' | 'Neutral'
  /** Polarita trendu MA5 vs. MA10 (#563); null dokud okna nejsou plná. */
  polarity?: 'up' | 'down' | null
  unconfirmed: boolean
  unconfirmed_state: string
  last_close: number | null
  /** σ(100 seancí) škály #640 — sparkline dělí hodnoty touto σ; null = málo historie. */
  sigma?: number | null
  ma5: number | null
  ma10: number | null
  threshold: number | null
  current_wave: {
    direction: string
    start_date: string
    end_date: string | null
    depth: number
    length_days: number
  } | null
}

export async function fetchSentimentState(symbol: string): Promise<SentimentStateInfo | null> {
  return getJson<SentimentStateInfo | null>(`/sentiment/state?symbol=${symbol}`, null)
}

/** Realizovaný výsledek signálu per okno (#294). */
export interface SignalOutcome {
  signal_id: number
  window_min: number
  ret_bp: number | string
  realized_dir: number | null
  correct: boolean | null
  computed_at: string
}

/** Signál Long/Short nápovědy (#294, SPEC 6.3) včetně zdůvodnění v `inputs`. */
export interface SignalRow {
  id: number
  ts: string
  symbol: string
  direction: 'long' | 'short'
  strength: number
  mode: 'NEWS' | 'COMBINED'
  inputs: Record<string, unknown>
  expiry_ts: string
  outcomes?: SignalOutcome[]
}

export async function fetchSignals(limit = 200): Promise<SignalRow[]> {
  const data = await getJson<{ signals: SignalRow[] }>(`/signals?limit=${limit}`, { signals: [] })
  // Generický mock/degradované API může vrátit objekt bez pole
  return data.signals ?? []
}

/** Řádek empirického modelu (`news_model_stats`) — podklad progresu ke gate (6.2). */
export interface ModelStatsRow {
  /** Režimový pohled (#402): all / RiskOn / RiskOff / Neutral / gamma_positive / gamma_negative. */
  regime: string
  category: string
  importance: number
  surprise_bucket: string
  deferred: boolean
  window_min: number
  symbol: string
  n: number
  ret_mean_bp: number
  hit_rate: number | null
  hit_rate_lb: number | null
}

export async function fetchNewsStats(): Promise<ModelStatsRow[]> {
  const data = await getJson<{ stats: ModelStatsRow[] }>('/news/stats', { stats: [] })
  return data.stats ?? []
}

/** Zrcadlo gate podmínky signal enginu (6.2): n ≥ 30 ∧ Wilson LB > 0.50. */
export const GATE_MIN_SAMPLES = 30
export const GATE_WILSON_LB = 0.5

export interface SignalGateInfo {
  /** Kolik bucketů primárního okna má gate otevřený. */
  open: number
  /** Nejlepší progres k n ≥ 30 (0–1) — pro stav „sbírám data". */
  progress: number
}

/** Progres ke gate z modelových statistik; primární okno +5 min (SPEC 6.2). */
export function signalGateInfo(
  stats: ModelStatsRow[],
  symbol: string,
  windowMin = 5,
): SignalGateInfo {
  let open = 0
  let progress = 0
  for (const row of stats) {
    // Progres ke gate se počítá z nepodmíněného pohledu (#402)
    if (row.regime !== undefined && row.regime !== 'all') continue
    if (row.window_min !== windowMin || row.symbol !== symbol) continue
    if (row.n >= GATE_MIN_SAMPLES && (row.hit_rate_lb ?? 0) > GATE_WILSON_LB) open += 1
    progress = Math.max(progress, Math.min(1, row.n / GATE_MIN_SAMPLES))
  }
  return { open, progress }
}

/** Vlna sentimentu (#292, SPEC 5.6) — řádek `sentiment_waves`. */
export interface WaveRow {
  id: number
  symbol: string
  direction: 'RiskOn' | 'RiskOff'
  start_date: string
  end_date: string | null
  depth: number
  /** Hloubka v jednotkách σ(100 seancí) (#640) — sjednocuje éry řady; null = σ nebyla. */
  depth_z?: number | null
  /** Verzování odvozené řady (#640): 'zscore_100'. */
  series_variant?: string | null
  length_days: number
}

export async function fetchWaves(): Promise<WaveRow[]> {
  const data = await getJson<{ waves: WaveRow[] }>('/stats/waves', { waves: [] })
  return data.waves ?? []
}

/** Bod equity křivky (#298, SPEC 7.3) — řádek `track_record`. */
export interface TrackRecordRow {
  date: string
  strategy: string
  symbol: string
  equity: number
  drawdown: number | null
}

export async function fetchTrackRecord(): Promise<TrackRecordRow[]> {
  const data = await getJson<{ track_record: TrackRecordRow[] }>('/stats/trackrecord', {
    track_record: [],
  })
  return data.track_record ?? []
}

/** Latence zdroje zpráv (#358): ts_ingested − ts_event, percentily + dávky. */
export interface SourceLatencyRow {
  source: string
  n: number
  /** Eventy nad stropem (staré články z prvního fetche, backfill) — mimo percentily. */
  n_over_cutoff: number
  median_s: number | null
  p90_s: number | null
  /** Podíl eventů doručených do 2 s od jiného — dávkované doručení. */
  batch_share: number | null
}

export async function fetchSourceLatency(
  days = 7,
): Promise<{ days: number; cutoff_s: number; latency: SourceLatencyRow[] }> {
  const data = await getJson<{ days: number; cutoff_s: number; latency: SourceLatencyRow[] }>(
    `/news/latency?days=${days}`,
    { days, cutoff_s: 0, latency: [] },
  )
  // Generický mock/degradované API může vrátit objekt bez pole
  return { days: data.days ?? days, cutoff_s: data.cutoff_s ?? 0, latency: data.latency ?? [] }
}

/** Položka review fronty (#293, SPEC 5.7). */
export interface ReviewRow {
  event_id: number
  reason: string
  created_at: string
  resolved_at: string | null
  title: string
  category: string | null
  sentiment_dir: number | null
}

export async function fetchReview(): Promise<ReviewRow[]> {
  const data = await getJson<{ review: ReviewRow[] }>('/review', { review: [] })
  return data.review
}

/** Ruční korekce směru/kategorie → nová verze klasifikace (source=manual). */
export async function submitReview(
  eventId: number,
  correction: { direction?: number; category?: string },
): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/review/${eventId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(correction),
    })
    return response.ok
  } catch {
    return false
  }
}
