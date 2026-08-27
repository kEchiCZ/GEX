/** Ranní briefing (#674): REST klient + čisté skládací helpery.

Obrazovka je ČISTÁ KOMPOZICE existujících dat — žádný nový výpočet v enginu.
Fetchery čtou lehké endpointy (/bars, /oidelta, /levels, /gexforward,
/gammacliff, /news/upcoming, /sentiment/state); helpery z barů skládají
overnight rozsah a včerejší settle. US open se počítá DST-korektně přes
zonedTimeUtc (#511).
*/
import { API_BASE } from '../config'
import { zonedTimeUtc } from '../instrument/tz'

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
  lines.push('- Teze dne: ')
  return lines.join('\n')
}
