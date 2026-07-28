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
  title: string
  summary: string | null
  sentiment_dir: number | null
  sentiment_score: number | null
  sentiment_source: string | null
  forecast: number | null
  previous: number | null
  actual: number | null
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

/** Naváže řadu sentimentu na časovou osu grafu (popisky HH:MM lokálně).

Index počítá news-engine nezávisle na tom, kdy dorazily bary, takže se hodnoty
párují podle času, ne podle pořadí. Minuty bez hodnoty drží poslední známou —
index je spojitý, ne vzorkovaný — a před první hodnotou je nula. */
export function alignSeriesToLabels(series: SentimentPoint[], labels: string[]): number[] {
  if (labels.length === 0) return []
  const byLabel = new Map<string, number>()
  for (const point of series) {
    const at = new Date(point.ts_min)
    const label = `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`
    byLabel.set(label, point.value)
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

export async function fetchNews(limit = 100): Promise<NewsRow[]> {
  const data = await getJson<{ news: NewsRow[] }>(`/news?limit=${limit}`, { news: [] })
  return data.news
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
