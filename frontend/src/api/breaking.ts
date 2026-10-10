/** Karta Breaking news (E-6.28c, ADR-0059 bod 4 a 8): kontrakt `GET /news/breaking`.

Výběr, potvrzení, skupinu, téma i dopad počítá API tímiž funkcemi jako
`news_reactions` (#1497) — frontend jen zobrazuje, žádné pravidlo nekopíruje.
*/
import { API_BASE } from '../config'

/** Stav dopadu na jeden symbol: běží do uzávěru posledního baru 5min okna,
pak zafixováno; zpráva při zavřeném trhu dopad nemá; `no_data` = mezera v barech. */
export type ImpactState = 'running' | 'fixed' | 'closed' | 'no_data'

export interface BreakingImpact {
  state: ImpactState
  elapsed_min: number
  /** Změna ceny od posledního close před zprávou (bp) — hlavní číslo. */
  ret_bp: number | null
  range_bp: number | null
  /** Výchylka ADR-0043 od minuty zprávy; null = zatím nejde změřit. */
  excursion_bp: number | null
  excursion_direction: number | null
  /** Do okna spadl jiný významný event jiné kategorie (K1). */
  contaminated: boolean
}

export interface BreakingSource {
  source: string
  content_tier: number | null
  published_at: string
  fetched_at: string
  /** Příjem − čas zprávy (s). */
  delay_s: number
}

export interface BreakingItem {
  id: number
  ts_event: string
  title: string
  is_key: boolean
  group: string
  theme: string | null
  /** Viditelné doručení tier 1–2; false = „článek, zatím nepotvrzeno“. */
  confirmed: boolean
  sources: BreakingSource[]
  impact: Record<string, BreakingImpact>
}

export interface BreakingCard {
  as_of: string
  live: boolean
  market_closed: boolean
  items: BreakingItem[]
}

export const BREAKING_SYMBOLS = ['ES', 'NQ'] as const

export const GROUP_LABELS: Record<string, string> = {
  macro: 'Makro data',
  central_banks: 'Centrální banky',
  geopolitics: 'Geopolitika',
  companies: 'Firmy',
  other: 'Ostatní',
}

/** Popisky témat (`breaking.THEMES`, ADR-0059 bod 4); neznámý klíč se ukáže syrově. */
export const THEME_LABELS: Record<string, string> = {
  fed: 'Fed',
  tariffs: 'Cla',
  iran: 'Írán',
  israel: 'Izrael',
  hormuz: 'Hormuz',
  middle_east: 'Blízký východ',
  china: 'Čína a Tchaj-wan',
  opec: 'OPEC',
  energy: 'Energie',
  inflation: 'Inflace',
  labor: 'Trh práce',
  growth: 'Růst',
  fiscal: 'Fiskál USA',
}

export function groupLabel(group: string): string {
  return GROUP_LABELS[group] ?? group
}

export function themeLabel(theme: string | null): string | null {
  if (theme === null) return null
  return THEME_LABELS[theme] ?? theme
}

/** Karta pro okamžik teď; chyba se vyhazuje — tiché prázdno by vypadalo jako klid. */
/** Okno a strop karty; při plném stropu UI řekne, že ukazuje jen nejnovější. */
export const BREAKING_HOURS = 12
export const BREAKING_LIMIT = 50

export async function fetchBreaking(
  hours = BREAKING_HOURS,
  limit = BREAKING_LIMIT,
): Promise<BreakingCard> {
  const response = await fetch(`${API_BASE}/news/breaking?hours=${hours}&limit=${limit}`)
  if (!response.ok) throw new Error(`news/breaking: HTTP ${response.status}`)
  const payload = (await response.json()) as BreakingCard
  if (!Array.isArray(payload.items)) throw new Error('news/breaking: odpověď bez pole items')
  return payload
}

/** Hodnota v bp se znaménkem; null = pomlčka. */
export function formatBp(value: number | null): string {
  if (value === null) return '—'
  return `${value >= 0 ? '+' : ''}${value.toFixed(1)} bp`
}

/** Text stavu dopadu: „běží 2 min“ / „5 min“ / „trh zavřený“ / „bez dat“. */
export function impactStateLabel(impact: BreakingImpact): string {
  switch (impact.state) {
    case 'running':
      return `běží ${impact.elapsed_min} min`
    case 'fixed':
      return '5 min'
    case 'closed':
      return 'trh zavřený'
    case 'no_data':
      return 'bez dat'
  }
}

/** Výchylka se směrem: „↑ 8.1 bp“; null = zatím nejde změřit. */
export function excursionLabel(impact: BreakingImpact): string | null {
  if (impact.excursion_bp === null) return null
  const arrow = impact.excursion_direction === -1 ? '↓' : '↑'
  return `${arrow} ${impact.excursion_bp.toFixed(1)} bp`
}

/** Zpoždění zdroje: „+4 s“, „+6 min“, „+2 h“. */
export function delayLabel(seconds: number): string {
  const value = Math.max(0, seconds)
  if (value < 60) return `+${Math.round(value)} s`
  if (value < 3600) return `+${Math.round(value / 60)} min`
  return `+${Math.round(value / 3600)} h`
}

/** Tooltip dopadu — odrážky a `\n` (vzor `ivRankTooltip`), ne odstavec. */
export function impactTooltip(symbol: string, impact: BreakingImpact): string {
  const lines = [
    `${symbol}: změna ceny od posledního close před zprávou (okno 5 min)`,
    impact.state === 'running'
      ? `• běží ${impact.elapsed_min} min, číslo se ještě mění`
      : '• zafixováno po 5 minutách',
  ]
  if (impact.state === 'closed') {
    lines[1] = '• zpráva přišla při zavřeném trhu; reakci na otevření ukáže feed zpráv'
  }
  if (impact.state === 'no_data') lines[1] = '• trh otevřený, ale bary po zprávě chybí'
  const excursion = excursionLabel(impact)
  if (excursion) lines.push(`• výchylka (high/low) od minuty zprávy: ${excursion}`)
  if (impact.contaminated) {
    lines.push('• ⚠ do okna spadla jiná významná zpráva — pohyb nejde přičíst jen této')
  }
  return lines.join('\n')
}

/** Tooltip štítku „nepotvrzeno“. */
export const UNCONFIRMED_TOOLTIP = [
  'Článek (tier 3), zatím bez doručení z oficiálního zdroje nebo headline feedu',
  '• štítek zmizí, až stejnou zprávu přinese tier 1–2',
  '• článek jde na kartu, jen když dorazil do 60 min od publikace',
].join('\n')
